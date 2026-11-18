import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

"""Forecast GTA VI launch-day Twitch CCU for hours 2-24.
Normalize 14-day pre-release viewer signatures, pick the 12 nearest comparable
launches, scale their release curves to the known hour-1 anchor, then expand the
median shape with lognormal quantiles (SIGMA=0.45, K=12, PRE_DAYS=14).
"""

import json
import math
import os
from datetime import datetime, timedelta
from statistics import median

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.644854, -0.674490, 0.0, 0.674490, 1.644854]
SIGMA = 0.45
K = 12
PRE_DAYS = 14
MIN_Q = 0.01
MAX_Q = 1_000_000_000.0


def parse_iso(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def round2(value):
    rounded = math.floor(value * 100.0 + 0.5) / 100.0
    return int(rounded) if rounded == int(rounded) else rounded


def _viewers(point):
    try:
        value = float(point.get("viewers", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _pre_raw(points, day_start_dt):
    raw = [0.0] * PRE_DAYS
    window_start = day_start_dt - timedelta(days=PRE_DAYS)
    for point in points or []:
        try:
            timestamp = parse_iso(point["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if window_start <= timestamp < day_start_dt:
            index = int((timestamp - window_start).total_seconds() // 86400.0)
            index = min(PRE_DAYS - 1, max(0, index))
            raw[index] += _viewers(point)
    return raw


def pre_signature(points, day_start_dt):
    raw = _pre_raw(points, day_start_dt)
    mean = sum(raw) / PRE_DAYS
    if mean <= 0:
        return [1.0 / PRE_DAYS] * PRE_DAYS
    return [value / mean for value in raw]


def release_curve(comparable):
    release_date = parse_iso(comparable["day_start"]).date()
    by_hour = {}
    for point in comparable.get("hourly") or []:
        try:
            timestamp = parse_iso(point["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp.date() == release_date:
            hour = timestamp.hour + 1
            if 1 <= hour <= 24:
                by_hour[hour] = _viewers(point)
    return [by_hour.get(hour, 0.0) for hour in range(1, 25)]


def _fallback_signature(comparables, game_name):
    target = str(game_name or "").strip().lower()
    for comparable in comparables:
        if str(comparable.get("name", "")).strip().lower() == target:
            return pre_signature(
                comparable.get("hourly") or [], parse_iso(comparable["day_start"])
            )
    for comparable in comparables:
        name = str(comparable.get("name", "")).lower()
        if "grand" in name or "gta" in name:
            return pre_signature(
                comparable.get("hourly") or [], parse_iso(comparable["day_start"])
            )
    return None


def forecast_case(case):
    levels = list(case.get("levels") or [])
    if levels != LEVELS:
        levels = list(LEVELS)

    known = case.get("known") or []
    hour_one = known[0] if known else {}
    try:
        anchor = float(hour_one.get("viewers", 1) or 1)
    except (TypeError, ValueError):
        anchor = 1.0
    anchor = max(1.0, anchor)
    if not math.isfinite(anchor):
        anchor = 1.0

    day_start_dt = parse_iso(case["day_start"])
    comparables = case.get("comparables") or []
    records = []
    for comparable in comparables:
        try:
            curve = release_curve(comparable)
            raw_pre = _pre_raw(comparable.get("hourly") or [], parse_iso(comparable["day_start"]))
            signature = pre_signature(
                comparable.get("hourly") or [], parse_iso(comparable["day_start"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        if curve[0] > 0 and any(value > 0 for value in raw_pre):
            records.append(
                (str(comparable.get("name", "")), signature, curve)
            )

    case_history = case.get("history_hourly") or []
    raw_case = _pre_raw(case_history, day_start_dt)
    signature_case = pre_signature(case_history, day_start_dt)
    if sum(1 for value in raw_case if value > 0) < 10:
        fallback = _fallback_signature(comparables, case.get("game", {}).get("name"))
        if fallback is not None:
            signature_case = fallback

    if records:
        def distance(record):
            return sum(abs(a - b) for a, b in zip(signature_case, record[1]))

        ordered = sorted(records, key=lambda record: (distance(record), record[0].lower()))
        selected = ordered[:K]
    else:
        selected = []

    hours = []
    last_ratio = 1.0
    for hour in range(2, 25):
        ratios = []
        for _, _, curve in selected:
            base = curve[0]
            if base > 0:
                ratio = curve[hour - 1] / base
                if ratio > 0 and math.isfinite(ratio):
                    ratios.append(ratio)
        ratio = median(ratios) if ratios else last_ratio
        last_ratio = ratio
        center = anchor * ratio
        if not math.isfinite(center) or center <= 0:
            center = anchor

        quantiles = []
        for z_value in Z:
            value = center * math.exp(SIGMA * z_value)
            value = min(MAX_Q, max(MIN_Q, value))
            quantiles.append(round2(value))
        for index in range(1, len(quantiles)):
            if quantiles[index] < quantiles[index - 1]:
                quantiles[index] = quantiles[index - 1]
        hours.append({"hour": hour, "q": quantiles})

    return hours


def main():
    input_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    output_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    for input_path in sorted(input_dir.glob("*.in")):
        with open(input_path, "r", encoding="utf-8") as handle:
            case = json.load(handle)
        result = {"hours": forecast_case(case)}
        output_name = str(case.get("id") or input_path.stem)
        output_name = Path(output_name).name
        payload = json.dumps(result, separators=(",", ":")) + "\n"
        with open(output_dir / (output_name + ".out"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)


if __name__ == "__main__":
    main()
