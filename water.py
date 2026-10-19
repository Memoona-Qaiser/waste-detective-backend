import numpy as np
import pandas as pd

WATER_TARIFF = 200       # Rs per m3 (andaza hai, apni utility ke hisaab se badlein)
CORE_HOURS = [1, 2, 3, 4]  # raat ka sab se shaant waqt (01:00 se 05:00)
MIN_LEAK_NIGHTS = 2      # kam az kam itni raatein masla raha ho


def simulate(cfg):
    """Water meter ka hourly flow (litre/ghanta), simulated.
    Din mein zyada istemal, raat ko lagbhag zero. Leak ho to flow kabhi band nahi hota."""
    rng = np.random.default_rng(cfg["seed"] + 100)
    idx = pd.date_range("2026-09-01", "2026-10-17 23:00", freq="h")
    df = pd.DataFrame({"timestamp": idx})
    df["hour"] = df.timestamp.dt.hour
    # Operational din: 06:00 se agle din 06:00 tak (electricity wali raat ki tareekh jaisi)
    df["night_date"] = (df.timestamp - pd.Timedelta(hours=6)).dt.normalize()

    base = np.select(
        [(df.hour >= 8) & (df.hour < 18), (df.hour >= 18) & (df.hour < 22)],
        [400.0, 150.0],
        default=5.0,
    )
    noise = rng.normal(0, 1, len(df)) * np.where(base > 100, 30.0, 2.0)
    df["flow"] = np.clip(base + noise, 0, None)

    leak = cfg.get("leak")
    if leak:
        m = (df.night_date >= pd.Timestamp(leak["start"])) & (df.night_date <= pd.Timestamp(leak["stop"]))
        df.loc[m, "flow"] += leak["rate"] + rng.normal(0, 6, int(m.sum()))
    return df


def detect(df, cfg, baseline_end, end, tariff_per_m3=WATER_TARIFF):
    """Minimum night flow method: raat ko jab koi paani istemal nahi karta,
    flow lagbhag zero hona chahiye. Agar kai raaton tak flow ek jaisa bana rahe, to leak ka shak hai."""
    core = df[df.hour.isin(CORE_HOURS)]
    nightly = core.groupby("night_date").flow.mean()  # har raat ka average flow

    train = nightly[nightly.index < baseline_end]
    base = float(train.median())
    sigma = max(float((train - train.median()).abs().median() * 1.4826), 0.5)

    window = nightly[(nightly.index >= baseline_end) & (nightly.index < end)]
    threshold = base + max(5 * sigma, 10.0)
    flagged = window[window > threshold]
    if len(flagged) < MIN_LEAK_NIGHTS:
        return None  # koi alert nahi: raat ka flow normal hai

    nights = int(len(flagged))
    measured = float(flagged.mean())
    leak_rate = measured - base                      # L/ghanta
    total_litres = leak_rate * 24 * nights           # leak 24 ghante chalta hai
    cost = total_litres / 1000 * tariff_per_m3

    # Confidence: kitni raatein, flow kitna ek jaisa, aur kitna bara farq
    cv = float(flagged.std(ddof=0) / flagged.mean()) if flagged.mean() > 0 else 1.0
    persistence = min(nights / 5, 1.0)
    consistency = 1.0 - min(cv / 0.5, 1.0)
    magnitude = min(leak_rate / (10 * max(sigma, 1.0)), 1.0)
    data_quality = 1.0
    score = round(min(100 * (0.4 * persistence + 0.3 * consistency + 0.2 * magnitude + 0.1 * data_quality), 95))
    label = "High" if score >= 75 else "Medium" if score >= 50 else "Low"

    # Chart: aakhri flagged raat, shaam 18:00 se agli subah 06:00 tak
    last = flagged.index.max()
    hour_base = df[df.timestamp < baseline_end].groupby("hour").flow.median()
    chart = df[
        (df.timestamp >= last + pd.Timedelta(hours=18))
        & (df.timestamp < last + pd.Timedelta(hours=30))
    ]
    hourly = [
        {
            "hour": f"{int(r.hour):02d}:00",
            "expected": round(float(hour_base[r.hour]), 1),
            "actual": round(float(r.flow), 1),
        }
        for r in chart.itertuples()
    ]

    evidence = [
        f"{nights} raaton mein raat ke sab se shaant ghanton (01:00 se 05:00) mein flow band hone ki jagah {round(measured, 1)} L/ghanta raha.",
        f"Normal behaviour mein is waqt flow lagbhag {round(base, 1)} L/ghanta hota hai.",
    ]
    if cv < 0.15:
        evidence.append("Flow raat bhar lagbhag ek jaisa raha. Ye leak ki nishani hai, kyunke insaani istemal ghatta barhta rehta hai.")
    evidence.append("Meter data se leak ki jagah confirm nahi hoti. Plumber ki inspection zaroori hai.")

    return {
        "id": 10 + cfg["id"],
        "resource": "water",
        "building": cfg["name"],
        "title": "Continuous night water flow (possible leak)",
        "status": "Inspection needed" if label != "Low" else "Uncertain: more data needed",
        "expected": round(base, 1),
        "measured": round(measured, 1),
        "excess_per_hour": round(leak_rate, 1),
        "unit": "L/ghanta",
        "total_excess": round(total_litres),
        "total_unit": "L",
        "flagged_hours": nights * len(CORE_HOURS),
        "nights_affected": nights,
        "potential_cost_rs": round(cost),
        "tariff": tariff_per_m3,
        "tariff_text": f"Rs {tariff_per_m3}/m3",
        "hourly": hourly,
        "confidence": label,
        "confidence_score": score,
        "confidence_parts": {
            "persistence": round(persistence, 2),
            "consistency": round(consistency, 2),
            "magnitude": round(magnitude, 2),
            "data_quality": data_quality,
        },
        "method": "Minimum night flow",
        "evidence": evidence,
        "action": "Pipeline, valves aur tanks ki plumber se inspection karwayein. Leak confirm ho to repair ke baad flow dobara naapein.",
        "note": "Zyada flow ka matlab confirmed leak nahi, jab tak inspection se tasdeeq na ho.",
        "task_title": "paani ki pipeline aur valves ki leak inspection karein",
        "simulated": True,
    }