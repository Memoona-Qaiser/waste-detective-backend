import copy
from functools import lru_cache

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth
import db
import explain
import ingest
import models
import water
import os

app = FastAPI(title="Resource Waste Detective API")
frontend_url = os.getenv(
    "FRONTEND_URL",
    "http://localhost:5173"
).rstrip("/")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        frontend_url,
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
db.init_db()

TARIFF = 60                                # Rs per kWh
NIGHT_HOURS = 8                            # 22:00 se 06:00
BASELINE_END = pd.Timestamp("2026-10-03")  # isse pehle ka data "normal" maana jayega
DATA_END = pd.Timestamp("2026-10-18")      # data 17 Oct 23:00 tak hai
MIN_AFTER_NIGHTS = 5                       # verification ke liye action ke baad kam az kam itni raatein
MIN_FLAGGED_HOURS = 6                      # isse kam flagged ghante hon to alert nahi banta

# Admin settings ki default values (database mein kuch save na ho to ye istemal hongi)
DEFAULTS = {
    "tariff": float(TARIFF),
    "water_tariff": float(water.WATER_TARIFF),
    "min_after_nights": float(MIN_AFTER_NIGHTS),
}


def _clean(v):
    """60.0 ko 60 banata hai, taake screen par 'Rs 60.0/kWh' na aaye."""
    return int(v) if float(v) == int(v) else float(v)


def apply_settings():
    """Database se settings parh kar un values par lagata hai jo detection istemal karti hai."""
    global TARIFF, MIN_AFTER_NIGHTS
    s = db.get_settings(DEFAULTS)
    TARIFF = _clean(s["tariff"])
    water.WATER_TARIFF = _clean(s["water_tariff"])
    MIN_AFTER_NIGHTS = int(s["min_after_nights"])
    return {
        "tariff": TARIFF,
        "water_tariff": water.WATER_TARIFF,
        "min_after_nights": MIN_AFTER_NIGHTS,
    }


apply_settings()


def require(*roles):
    """Endpoint par lagta hai: login zaroori, aur role in roles mein se ek ho."""
    def check(authorization: str = Header(default="")):
        user = auth.user_from_header(authorization)
        if user is None:
            raise HTTPException(401, "Pehle login karein")
        if user["role"] not in roles:
            raise HTTPException(403, f"Ye kaam sirf {' ya '.join(roles)} kar sakta hai")
        return user
    return check


# Har building ke paas bijli aur paani dono ke meters hain (simulated)
BUILDINGS = [
    {   # bijli ka bara waste, 10 Oct ko fix hua
        "id": 1, "name": "Building A", "seed": 42, "night_base": 8, "day_base": 28,
        "waste": [("2026-10-03", "2026-10-09", 12.0)],
        "fix_date": "2026-10-10", "residual": 1.5, "leak": None,
    },
    {   # bijli ka halka, ek raat ka masla (kam confidence)
        "id": 2, "name": "Building B", "seed": 7, "night_base": 15, "day_base": 45,
        "waste": [("2026-10-15", "2026-10-15", 5.0)],
        "fix_date": None, "residual": 0, "leak": None,
    },
    {   # bijli normal, lekin paani ki leak (5 se 11 Oct), 12 Oct ko repair hui
        "id": 3, "name": "Building C", "seed": 11, "night_base": 10, "day_base": 30,
        "waste": [], "fix_date": None, "residual": 0,
        "leak": {"start": "2026-10-05", "stop": "2026-10-11", "rate": 250.0, "fix_date": "2026-10-12"},
    },
    {   # sab normal
        "id": 4, "name": "Building D", "seed": 21, "night_base": 12, "day_base": 35,
        "waste": [], "fix_date": None, "residual": 0, "leak": None,
    },
]


def simulate_building(cfg):
    """Bijli ka hourly data, 1 Sep se 17 Oct tak (simulated)."""
    rng = np.random.default_rng(cfg["seed"])
    idx = pd.date_range("2026-09-01", "2026-10-17 23:00", freq="h")
    df = pd.DataFrame({"timestamp": idx})
    df["hour"] = df.timestamp.dt.hour
    df["night"] = (df.hour >= 22) | (df.hour < 6)
    # Raat ki tareekh: 22:00-23:59 aur agle din ke 00:00-05:59 ek hi raat ke hain
    df["night_date"] = (df.timestamp - pd.Timedelta(hours=6)).dt.normalize()

    base = np.where((df.hour >= 8) & (df.hour < 18), cfg["day_base"], cfg["night_base"])
    df["kwh"] = base + rng.normal(0, 1, len(df))

    is_waste = pd.Series(False, index=df.index)
    for start, stop, extra in cfg["waste"]:
        m = df.night & (df.night_date >= pd.Timestamp(start)) & (df.night_date <= pd.Timestamp(stop))
        df.loc[m, "kwh"] += extra
        is_waste |= m
    if cfg["fix_date"]:
        fixed = df.night & (df.night_date >= pd.Timestamp(cfg["fix_date"]))
        df.loc[fixed, "kwh"] += cfg["residual"]  # fix ke baad bhi thora residual usage
    df["is_waste"] = is_waste                      # sirf model evaluation ke liye (simulated labels)
    return df


def window_end(cfg):
    return pd.Timestamp(cfg["fix_date"]) if cfg["fix_date"] else DATA_END


def detect_after_hours(df, cfg):
    baseline = df[df.timestamp < BASELINE_END].groupby("hour").kwh.median()
    end = window_end(cfg)
    window = df[df.night & (df.night_date >= BASELINE_END) & (df.night_date < end)].copy()
    window["expected"] = window.hour.map(baseline)
    window["excess"] = window.kwh - window.expected
    flagged = window[window.excess > 3]
    if len(flagged) < MIN_FLAGGED_HOURS:
        return None  # koi alert nahi: ye normal usage hai

    expected = float(flagged.expected.mean())
    measured = float(flagged.kwh.mean())
    total_excess = float(flagged.excess.sum())
    nights = int(flagged.night_date.nunique())

    # Chart: aakhri flagged raat, shaam 18:00 se agli subah 06:00 tak
    last = flagged.night_date.max()
    chart = df[
        (df.timestamp >= last + pd.Timedelta(hours=18))
        & (df.timestamp < last + pd.Timedelta(hours=30))
    ].copy()
    chart["expected"] = chart.hour.map(baseline)
    hourly = [
        {
            "hour": f"{int(r.hour):02d}:00",
            "expected": round(float(r.expected), 1),
            "actual": round(float(r.kwh), 1),
        }
        for r in chart.itertuples()
    ]

    return {
        "id": cfg["id"],
        "resource": "electricity",
        "building": cfg["name"],
        "title": "After-hours electricity usage",
        "status": "Investigation needed",
        "expected": round(expected, 1),
        "measured": round(measured, 1),
        "excess_per_hour": round(measured - expected, 1),
        "unit": "kWh/ghanta",
        "total_excess": round(total_excess, 1),
        "total_unit": "kWh",
        "flagged_hours": int(len(flagged)),
        "nights_affected": nights,
        "potential_cost_rs": round(total_excess * TARIFF),
        "tariff": TARIFF,
        "tariff_text": f"Rs {TARIFF}/kWh",
        "hourly": hourly,
        "evidence": [
            f"{nights} raaton mein 22:00 se 06:00 ke beech usage expected se zyada raha.",
            f"Average usage {round(measured, 1)} kWh/ghanta tha, jabke pehle ke normal behaviour ke mutabiq {round(expected, 1)} kWh/ghanta expected tha.",
            "Operating schedule (08:00 se 18:00) ke mutabiq us waqt occupancy expected nahi thi.",
        ],
        "action": "Equipment schedules aur meter readings inspect karo. Agar unnecessary usage confirm ho, schedule adjustment ke baad savings verify karo.",
        "note": "Excess consumption hai, confirmed waste nahi. Inspection ke baad confirm hoga.",
        "task_title": "equipment schedule inspect karein aur adjust karein",
        "simulated": True,
    }


def nightly_series(df, resource):
    """Har raat ka average (sirf poori raatein). Bijli: 22:00-06:00, paani: 01:00-05:00."""
    if resource == "water":
        g = df[df.hour.isin(water.CORE_HOURS)].groupby("night_date").flow.agg(["mean", "count"])
        full = len(water.CORE_HOURS)
    else:
        g = df[df.night].groupby("night_date").kwh.agg(["mean", "count"])
        full = NIGHT_HOURS
    return g[g["count"] == full]["mean"]


def build_verification(df, alert, meta):
    """Action ke baad ka data kaafi ho to before/after compare karta hai,
    warna 'continue monitoring' batata hai."""
    resource = meta["resource"]
    if not meta["fix_date"]:
        return {
            "insufficient": True,
            "nights_after": 0,
            "required_nights": MIN_AFTER_NIGHTS,
            "message": (
                "Kaam complete ho chuka hai, lekin action ke baad ka comparable data abhi maujood nahi. "
                f"Savings verify karne ke liye kam az kam {MIN_AFTER_NIGHTS} raaton ka data chahiye. "
                "Monitoring jaari hai, koi savings confirm nahi ki ja sakti."
            ),
        }
    action = pd.Timestamp(meta["fix_date"])

    nights = nightly_series(df, resource)
    before = nights[(nights.index >= BASELINE_END) & (nights.index < action)]
    after = nights[nights.index >= action]
    if len(after) < MIN_AFTER_NIGHTS or len(before) < MIN_AFTER_NIGHTS:
        return {
            "insufficient": True,
            "nights_after": int(len(after)),
            "required_nights": MIN_AFTER_NIGHTS,
            "message": f"Action ke baad sirf {len(after)} raatein hain. Kam az kam {MIN_AFTER_NIGHTS} chahiye. Monitoring jaari hai.",
        }

    n = min(len(before), len(after), 7)
    before, after = before.tail(n), after.head(n)

    reduction = float(before.mean() - after.mean())
    verified_per_period = round(reduction * meta["hours"] * meta["rs_per_unit"])
    estimated_per_period = round(alert["excess_per_hour"] * meta["hours"] * meta["rs_per_unit"])

    if resource == "water":
        checks = [
            f"Repair se pehle aur baad ki {n} raaton ka raat (01:00 se 05:00) ka flow compare hua.",
            "Din ka istemal alag ho sakta hai, is liye sirf raat ka shaant waqt dekha gaya jab koi paani istemal nahi karta.",
            "Repair ke baad flow normal level ke qareeb aaya. Isse leak band hone ki tasdeeq hoti hai.",
        ]
    else:
        checks = [
            f"Action se pehle aur baad ki {n} raatein ek jaise ghanton (22:00 se 06:00) par compare hui.",
            "Weather aur occupancy ka farq is simulated data mein model nahi kiya gaya. Real data mein ye adjust karna hoga.",
            "Action ke baad usage expected level ke kareeb aaya, magar abhi thora zyada hai.",
        ]

    # Kya verified bachat pehle batayi gayi range ke andar rahi? (ek raat ya ek din ke hisaab se)
    u = alert.get("savings_uncertainty", 0.15)
    range_low = round(estimated_per_period * (1 - u))
    in_range = range_low <= verified_per_period <= estimated_per_period
    unit_word = "raat" if meta["period"] == "raat" else "din"
    checks.append(
        f"Verified bachat Rs {verified_per_period:,} per {unit_word} thi, jabke pehle ka andaza Rs {range_low:,} se "
        f"Rs {estimated_per_period:,} tha. Nateeja is range ke "
        + ("andar raha." if in_range else "bahar raha, is liye andaze ko dobara dekhna chahiye.")
    )

    return {
        "insufficient": False,
        "within_range": in_range,
        "before": [round(float(x), 1) for x in before],
        "after": [round(float(x), 1) for x in after],
        "expected": alert["expected"],
        "unit": alert["unit"],
        "total_unit": alert["total_unit"],
        "period": meta["period"],
        "period_plural": meta["period_plural"],
        "reduction_per_hour": round(reduction, 1),
        "estimated_per_night_rs": estimated_per_period,
        "verified_per_night_rs": verified_per_period,
        "nights_after": int(n),
        "verified_total_rs": verified_per_period * int(n),
        "checks": checks,
    }


class Review(BaseModel):
    decision: str  # valid | false_alarm | needs_data
    reason: str = ""
    actor: str = "Manager"  # ab ignore hota hai, asli naam login se aata hai


class Approval(BaseModel):
    approve: bool
    reason: str = ""
    assignee: str = "Technician 1"
    priority: str = "High"
    due_date: str = ""
    actor: str = "Manager"  # ab ignore hota hai


class Completion(BaseModel):
    note: str
    actor: str = "Technician 1"  # ab ignore hota hai


class SettingsIn(BaseModel):
    tariff: float
    water_tariff: float
    min_after_nights: int
    actor: str = "Manager"  # ab ignore hota hai


class LoginIn(BaseModel):
    username: str
    password: str


def add_savings_range(a):
    """Excess cost ko bachat ki range mein badalta hai.
    Maximum = poora excess hat jaye. Minimum = excess ka kuch hissa legitimate ho sakta hai;
    confidence jitni kam, gunjaish utni zyada."""
    uncertainty = 0.15 + 0.5 * (1 - a["confidence_score"] / 100)
    top = a["potential_cost_rs"]
    low = round(top * (1 - uncertainty))
    a["savings_min_rs"] = low
    a["savings_max_rs"] = top
    a["savings_uncertainty"] = round(uncertainty, 3)
    if a["resource"] == "water":
        extra = "Leak ki jagah meter se confirm nahi hoti. Repair ka kharcha is range mein shamil nahi."
    else:
        extra = "Maintenance ya special events ka hisaab nahi kiya gaya, raat ka sara extra usage unnecessary maana gaya."
    a["savings_assumptions"] = [
        "Maximum bachat tab hai jab poora excess hat jaye.",
        f"Minimum bachat excess ka {round((1 - uncertainty) * 100)}% hai: confidence {a['confidence_score']}/100 ke hisaab se {round(uncertainty * 100)}% ki gunjaish rakhi gayi (confidence kam to gunjaish zyada).",
        f"Tariff: {a['tariff_text']} (andaza hai, apni utility ke hisaab se badla ja sakta hai).",
        extra,
        "Data simulated hai. Asli data par range badal sakti hai.",
    ]


@lru_cache(maxsize=1)
def _compute():
    """Simulation aur models ek hi baar chalte hain (data static hai).
    Settings badalne par cache_clear() se dobara chalte hain."""
    items = []
    for cfg in BUILDINGS:
        # 1. Bijli
        df = simulate_building(cfg)
        alert = detect_after_hours(df, cfg)
        if alert is not None:
            # AI models: comparison aur confidence
            result = models.analyze(df, BASELINE_END, window_end(cfg))
            alert.update(result)
            agree = round(result["confidence_parts"]["agreement"] * 100)
            alert["evidence"].append(
                f"Do alag methods (median baseline aur Isolation Forest) ne flagged ghanton ke {agree}% par ittefaq kiya."
            )
            alert["method"] = "Median baseline + Isolation Forest"
            if result["confidence"] == "Low":
                alert["status"] = "Uncertain: more data needed"
            meta = {
                "resource": "electricity", "fix_date": cfg["fix_date"], "hours": NIGHT_HOURS,
                "rs_per_unit": TARIFF, "period": "raat", "period_plural": "raatein",
            }
            items.append((meta, df, alert))

        # 2. Paani
        wdf = water.simulate(cfg)
        leak = cfg["leak"]
        wend = pd.Timestamp(leak["fix_date"]) if leak and leak.get("fix_date") else DATA_END
        walert = water.detect(wdf, cfg, BASELINE_END, wend)
        if walert is not None:
            meta = {
                "resource": "water", "fix_date": leak["fix_date"] if leak else None, "hours": 24,
                "rs_per_unit": water.WATER_TARIFF / 1000, "period": "din", "period_plural": "din",
            }
            items.append((meta, wdf, walert))

    for _, _, a in items:
        # Priority: bachne wali cost ko confidence se tolna
        a["priority_score"] = round(a["potential_cost_rs"] * a["confidence_score"] / 100)
        add_savings_range(a)
    items.sort(key=lambda x: x[2]["priority_score"], reverse=True)
    for rank, (_, _, a) in enumerate(items, start=1):
        a["rank"] = rank
    return items


def current_alerts():
    items = []
    for meta, df, alert in _compute():
        a = copy.deepcopy(alert)
        a.update(db.get_state(a["id"]))  # state aur reason database se
        items.append((meta, df, a))
    return items


def find_alert(alert_id):
    for _, _, a in current_alerts():
        if a["id"] == alert_id:
            return a
    raise HTTPException(404, "Alert nahi mila")


@app.post("/api/login")
def login(body: LoginIn):
    user = auth.verify(body.username, body.password)
    if user is None:
        db.log("System", "Login failed", f"username: {body.username[:40]}")
        raise HTTPException(401, "Username ya password galat hai")
    db.log(user["name"], "Login", f"role: {user['role']}")
    return {"token": auth.make_token(user["username"]), "user": user}


@app.get("/api/overview")
def get_overview():
    with_alert = {a["building"] for _, _, a in current_alerts()}
    return {
        "monitored": len(BUILDINGS),
        "normal": [c["name"] for c in BUILDINGS if c["name"] not in with_alert],
    }


@app.get("/api/alerts")
def get_alerts():
    return [a for _, _, a in current_alerts()]


@app.post("/api/alerts/{alert_id}/review")
def review_alert(alert_id: int, body: Review, user: dict = Depends(require("Manager"))):
    alert = find_alert(alert_id)
    if alert["state"] not in ("new", "needs_data"):
        raise HTTPException(400, "Is alert ka review pehle ho chuka hai")

    if body.decision == "valid":
        new_state = "valid_issue"
    elif body.decision == "false_alarm":
        if not body.reason.strip():
            raise HTTPException(400, "False alarm ki wajah likhna zaroori hai")
        new_state = "false_alarm"
    elif body.decision == "needs_data":
        if alert["state"] == "needs_data":
            raise HTTPException(
                400,
                "Is alert par pehle se 'Needs more data' mark hai. Naya data aane ke baad Valid issue ya False alarm chunein.",
            )
        if not body.reason.strip():
            raise HTTPException(400, "Kaun sa data ya inspection chahiye, wajah likhna zaroori hai")
        new_state = "needs_data"
    else:
        raise HTTPException(400, "decision: valid, false_alarm ya needs_data hona chahiye")

    db.set_state(alert_id, new_state, body.reason)
    db.log(user["name"], f"Review: {body.decision}", f"Alert {alert_id}. {body.reason}")
    return db.get_state(alert_id)


@app.post("/api/alerts/{alert_id}/approve")
def approve_action(alert_id: int, body: Approval, user: dict = Depends(require("Manager"))):
    alert = find_alert(alert_id)
    if alert["state"] != "valid_issue":
        raise HTTPException(400, "Pehle alert ko valid issue mark karein")

    if not body.approve:
        if not body.reason.strip():
            raise HTTPException(400, "Defer ya reject ki wajah likhna zaroori hai")
        db.set_state(alert_id, "deferred", body.reason)
        db.log(user["name"], "Action deferred", f"Alert {alert_id}. {body.reason}")
        return {"state": "deferred"}

    task_id = db.create_task(
        alert_id,
        f"{alert['building']}: {alert['task_title']}",
        body.assignee,
        body.priority,
        body.due_date,
    )
    db.set_state(alert_id, "task_assigned")
    db.log(
        user["name"],
        "Action approved, task assigned",
        f"Alert {alert_id}, task {task_id}, {body.assignee}, priority {body.priority}",
    )
    return {"state": "task_assigned", "task_id": task_id}


@app.get("/api/tasks")
def get_tasks():
    return db.list_tasks()


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: int, body: Completion, user: dict = Depends(require("Technician"))):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task nahi mila")
    if task["status"] == "done":
        raise HTTPException(400, "Ye task pehle hi complete ho chuka hai")
    if not body.note.strip():
        raise HTTPException(400, "Kaam ka note ya evidence likhna zaroori hai")

    db.complete_task(task_id, body.note)
    db.set_state(task["alert_id"], "completed")
    db.log(user["name"], "Task completed", f"Task {task_id}. {body.note}")
    return {"state": "completed"}


@app.get("/api/verification")
def get_verification():
    # Verification sirf unhi alerts ki, jin ka task complete ho chuka hai
    return {
        str(a["id"]): build_verification(df, a, meta)
        for meta, df, a in current_alerts()
        if a["state"] == "completed"
    }


@app.get("/api/audit")
def get_audit():
    return db.list_audit()


@app.post("/api/demo/reset")
def demo_reset(user: dict = Depends(require("Manager"))):
    db.reset()
    return {"ok": True}


class Upload(BaseModel):
    filename: str = "upload.csv"
    csv: str


@app.post("/api/upload")
def upload_csv(body: Upload):
    result = ingest.analyze_csv(body.csv, TARIFF)
    if result["ok"]:
        db.log("System", "Data upload accepted", f"{body.filename}: {result['stats']['hours_used']} ghante")
    else:
        db.log("System", "Data upload rejected", f"{body.filename}: {result['errors'][0]}")
    return result


@app.get("/api/alerts/{alert_id}/explain")
def explain_alert(alert_id: int):
    return explain.explain(find_alert(alert_id))


@app.get("/api/settings")
def get_settings():
    return {
        "tariff": TARIFF,
        "water_tariff": water.WATER_TARIFF,
        "min_after_nights": MIN_AFTER_NIGHTS,
    }


@app.post("/api/settings")
def save_settings(body: SettingsIn, user: dict = Depends(require("Manager"))):
    if not (1 <= body.tariff <= 1000):
        raise HTTPException(400, "Bijli ka tariff 1 se 1000 Rs/kWh ke beech hona chahiye")
    if not (1 <= body.water_tariff <= 5000):
        raise HTTPException(400, "Paani ka tariff 1 se 5000 Rs/m3 ke beech hona chahiye")
    if not (2 <= body.min_after_nights <= 14):
        raise HTTPException(400, "Verification ke liye raatein 2 se 14 ke beech honi chahiye")

    db.set_settings({
        "tariff": body.tariff,
        "water_tariff": body.water_tariff,
        "min_after_nights": body.min_after_nights,
    })
    new = apply_settings()
    _compute.cache_clear()  # alerts, range aur ranking naye tariff se dobara banenge
    db.log(
        user["name"],
        "Settings changed",
        f"Bijli Rs {new['tariff']}/kWh, paani Rs {new['water_tariff']}/m3, kam az kam {new['min_after_nights']} raatein",
    )
    return new