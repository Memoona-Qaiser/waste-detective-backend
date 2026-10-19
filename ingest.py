import io

import numpy as np
import pandas as pd

TS_NAMES = ["timestamp", "time", "datetime", "date_time", "date"]
VAL_NAMES = ["kwh", "energy_kwh", "consumption", "usage", "meter_reading", "value"]
MIN_HOURS = 14 * 24  # kam az kam 14 din ka data


def _fail(report, msg):
    report["errors"].append(msg)
    return report


def analyze_csv(text, tariff=60):
    """CSV ko validate karta hai (schema, timestamps, units, missing/duplicate data),
    phir saaf data par after-hours waste dhoondta hai."""
    report = {"ok": False, "errors": [], "warnings": [], "stats": {}, "analysis": None}

    if not text or not text.strip():
        return _fail(report, "File khali hai.")
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception:
        return _fail(report, "CSV parh nahi saki. Comma se alag columns wali file chahiye.")

    # 1. Schema
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    ts_col = next((c for c in TS_NAMES if c in raw.columns), None)
    val_col = next((c for c in VAL_NAMES if c in raw.columns), None)
    if ts_col is None or val_col is None:
        return _fail(
            report,
            "Zaroori columns nahi mile. 'timestamp' aur 'kwh' columns chahiye "
            f"(aap ki file ke columns: {', '.join(raw.columns)}).",
        )

    # 2. Timestamps aur values
    ts = pd.to_datetime(raw[ts_col], errors="coerce")
    val = pd.to_numeric(raw[val_col], errors="coerce")
    bad_ts = int(ts.isna().sum())
    bad_val = int((val.isna() & ts.notna()).sum())
    df = pd.DataFrame({"timestamp": ts, "kwh": val}).dropna()
    if bad_ts:
        report["warnings"].append(f"{bad_ts} rows ka timestamp galat tha, hata diye gaye.")
    if bad_val:
        report["warnings"].append(f"{bad_val} rows mein kwh number nahi tha, hata diye gaye.")
    if len(df) < 3:
        return _fail(report, "Sahi rows bohat kam hain (3 se kam).")

    negatives = int((df.kwh < 0).sum())
    if negatives:
        report["warnings"].append(f"{negatives} rows mein negative kWh tha (sensor error), hata diye gaye.")
        df = df[df.kwh >= 0]

    duplicates = int(df.timestamp.duplicated().sum())
    if duplicates:
        report["warnings"].append(f"{duplicates} duplicate timestamps mile, ek ek rakha gaya.")
        df = df.drop_duplicates("timestamp")

    df = df.sort_values("timestamp")
    if len(df) < 3:
        return _fail(report, "Saaf karne ke baad rows bohat kam bache.")

    # 3. Interval
    step = df.timestamp.diff().dropna().median()
    if step > pd.Timedelta(hours=1):
        return _fail(report, "Readings ka interval ek ghante se bara hai. Hourly (ya usse chhota interval) data chahiye.")
    if step < pd.Timedelta(hours=1):
        report["warnings"].append(
            f"Readings {int(step.total_seconds() // 60)} minute ke interval par hain, hourly mein jama kar di gayin."
        )
    series = df.set_index("timestamp").kwh.resample("h").sum(min_count=1)

    # 4. Units
    if float(series.median()) > 5000:
        report["warnings"].append("Values bohat bari hain. Shayad units Wh hain, kWh nahi. Check kar lein.")

    # 5. Missing hours
    missing = int(series.isna().sum())
    if missing / len(series) > 0.10:
        return _fail(report, f"{missing} ghante missing hain (10% se zyada). Data quality bohat kam hai, dobara upload karein.")
    series = series.interpolate(limit=3, limit_area="inside")
    remaining = int(series.isna().sum())
    if missing:
        report["warnings"].append(
            f"{missing} ghante missing the. {missing - remaining} chhote gaps bhare gaye, {remaining} analysis se bahar rakhe gaye."
        )

    if len(series) < MIN_HOURS:
        return _fail(report, f"Kam az kam 14 din ka data chahiye. Aap ki file mein {len(series) // 24} din hain.")

    report["stats"] = {
        "rows_in_file": int(len(raw)),
        "hours_used": int(series.notna().sum()),
        "start": series.index.min().isoformat(),
        "end": series.index.max().isoformat(),
        "days": int(len(series) // 24),
        "missing_hours": missing,
        "duplicates_removed": duplicates,
        "negatives_removed": negatives,
        "invalid_rows_removed": bad_ts + bad_val,
    }
    report["ok"] = True
    report["analysis"] = _analyze(series.dropna(), tariff)
    return report


def _analyze(series, tariff):
    d = series.rename("kwh").to_frame()
    d["hour"] = d.index.hour
    d["night"] = (d.hour >= 22) | (d.hour < 6)
    d["night_date"] = (d.index - pd.Timedelta(hours=6)).normalize()

    # Pehla 70% data normal behaviour seekhne ke liye, baqi test ke liye
    split = d.index[int(len(d) * 0.7)]
    train = d[d.index < split]
    test = d[d.index >= split].copy()

    overall = float(train.kwh.median())
    base = train.groupby("hour").kwh.median().reindex(range(24)).fillna(overall)
    sigma = (
        train.groupby("hour").kwh.apply(lambda s: (s - s.median()).abs().median() * 1.4826)
        .reindex(range(24))
        .fillna(1.0)
        .clip(lower=0.5)
    )

    test["expected"] = test.hour.map(base)
    test["excess"] = test.kwh - test.expected
    threshold = np.maximum(4 * test.hour.map(sigma), 0.15 * test.expected)
    flagged = test[test.night & (test.excess > threshold)]

    recent_mean = test.groupby("hour").kwh.mean()
    profile = [
        {
            "hour": f"{h:02d}:00",
            "baseline": round(float(base[h]), 1),
            "recent": round(float(recent_mean.get(h, base[h])), 1),
        }
        for h in range(24)
    ]

    out = {
        "baseline_days": int(len(train) // 24),
        "test_days": int(len(test) // 24),
        "profile": profile,
        "tariff": tariff,
    }
    if flagged.empty:
        out.update(
            anomaly_found=False,
            message="After-hours mein koi unusual usage nahi mili. Ye normal usage record hui.",
        )
    else:
        total = float(flagged.excess.sum())
        out.update(
            anomaly_found=True,
            flagged_hours=int(len(flagged)),
            nights_affected=int(flagged.night_date.nunique()),
            expected_kwh=round(float(flagged.expected.mean()), 1),
            measured_kwh=round(float(flagged.kwh.mean()), 1),
            total_excess_kwh=round(total, 1),
            potential_cost_rs=round(total * tariff),
            message="Excess consumption mili hai, confirmed waste nahi. Inspection ke baad confirm hoga.",
        )
    return out