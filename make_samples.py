import csv
import random
from datetime import datetime, timedelta

random.seed(42)

START = datetime(2026, 9, 1, 0, 0)
DAYS = 30


def kwh_for(ts, waste=False):
    hour = ts.hour
    if 8 <= hour < 18:
        base = 22
    elif 6 <= hour < 8 or 18 <= hour < 22:
        base = 12
    else:
        base = 8
    if waste and (hour >= 22 or hour < 6):
        base += 12
    return round(base + random.uniform(-1.0, 1.0), 2)


def build_rows():
    rows = []
    for i in range(DAYS * 24):
        ts = START + timedelta(hours=i)
        waste = ts.day >= 22
        rows.append([ts.strftime("%Y-%m-%d %H:%M:%S"), kwh_for(ts, waste)])
    return rows


def write(name, header, rows):
    with open(name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print("bana:", name, len(rows), "rows")


# 1) Saaf file
good = build_rows()
write("sample_good.csv", ["timestamp", "kwh"], good)

# 2) Ganda data: negative, duplicate, missing hours, text value
bad = [r[:] for r in good]
bad[50][1] = -5
bad[120][1] = "abc"
bad.insert(81, bad[80][:])
for idx in (300, 301, 302):
    bad[idx] = None
bad = [r for r in bad if r is not None]
write("sample_bad.csv", ["timestamp", "kwh"], bad)

# 3) Galat column names
wrong = [[r[0], r[1]] for r in good]
write("sample_wrong.csv", ["tarikh", "meter_value"], wrong)