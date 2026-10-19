import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

THRESHOLD_KWH = 3  # median baseline: itna excess ho to flag


def _features(d):
    angle = 2 * np.pi * d.hour / 24
    return np.column_stack([np.sin(angle), np.cos(angle), d.kwh.values])


def _scores(pred, truth):
    tp = int((pred & truth).sum())
    fp = int((pred & ~truth).sum())
    fn = int((~pred & truth).sum())
    return {
        "flagged": tp + fp,
        "false_alarms": fp,
        "missed": fn,
        "precision": round(tp / max(tp + fp, 1), 3),
        "recall": round(tp / max(tp + fn, 1), 3),
    }


def analyze(df, baseline_end, action_date):
    train = df[df.timestamp < baseline_end]
    test = df[df.timestamp >= baseline_end].copy()

    # Kam history ho to model chalana ghalat hoga: "insufficient data" wapas do
    if len(train) < 24 * 7 or test.empty:
        return {
            "models": None,
            "confidence_score": 0,
            "confidence": "Low",
            "confidence_parts": {"agreement": 0, "persistence": 0, "data_quality": 0},
            "anomaly_score": 0,
        }

    baseline = train.groupby("hour").kwh.median()
    test["median_flag"] = ((test.kwh - test.hour.map(baseline)) > THRESHOLD_KWH).values

    iso = IsolationForest(n_estimators=200, contamination=0.03, random_state=42)
    iso.fit(_features(train))
    test["iso_flag"] = iso.predict(_features(test)) == -1
    test["iso_score"] = -iso.score_samples(_features(test))

    test["both_flag"] = test.median_flag & test.iso_flag

    truth = test.is_waste.values
    models = {
        "median": _scores(test.median_flag.values, truth),
        "isolation_forest": _scores(test.iso_flag.values, truth),
        "ensemble": _scores(test.both_flag.values, truth),
        "eval_hours": int(len(test)),
        "true_waste_hours": int(truth.sum()),
    }

    window = test[
        test.night
        & (test.night_date >= baseline_end)
        & (test.night_date < action_date)
        & test.median_flag
    ]
    n = max(len(window), 1)
    agreement = float(window.iso_flag.sum()) / n
    nights = int(window.night_date.nunique())
    persistence = min(nights / 5, 1.0)
    data_quality = 1.0

    score = round(
        min(100 * (0.5 * agreement + 0.3 * persistence + 0.2 * data_quality), 95)
    )
    label = "High" if score >= 75 else "Medium" if score >= 50 else "Low"

    return {
        "models": models,
        "confidence_score": score,
        "confidence": label,
        "confidence_parts": {
            "agreement": round(agreement, 2),
            "persistence": round(persistence, 2),
            "data_quality": data_quality,
        },
        "anomaly_score": round(float(window.iso_score.mean()), 2) if len(window) else 0,
    }