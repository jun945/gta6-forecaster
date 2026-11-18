"""Forecast GTA6 Twitch concurrency from one known launch-hour print.

Each competition case supplies the hour-1 audience plus a set of past
launches ("comparables") with hourly audience histories.  For every
comparable we build a release curve curve[1..24]: curve[h] is the total
audience at hour-of-day h-1 on that comparable's launch date, so hour 1 is
hour 0 of the launch day and hour 24 is hour 23.

The forecast is an hour-1 kernel over those curves.  The target's known
hour-1 audience anchors the level, and each comparable is weighted by

    w = exp(-(log(anchor / curve[1])) ** 2 / (2 * 0.6 ** 2))

so launches with a similar opening hour dominate.  The log-scale kernel
width of 0.6 makes that neighborhood scale-free: openings within roughly a
factor of exp(0.6) ~ 1.8 of the anchor still carry meaningful weight.  For
each future hour we take the weighted mean of the comparable
hour-to-hour-1 ratios and scale the anchor by it.  Multiplicative uncertainty z * 0.35 * sqrt(h-1) yields the
5th/25th/50th/75th/95th percentile band, clamped to positive values and
forced non-decreasing over time.  Each case <id> becomes
<MAKEFUN_OUTPUT_DIR>/<id>.out containing {"hours": [...]}.
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

Z5 = (-1.644854, -0.674490, 0.0, 0.674490, 1.644854)
KERNEL_WIDTH = 0.6
SPREAD_PER_SQRT_HOUR = 0.35
MIN_WEIGHT_SUM = 1e-9
MIN_Q = 0.01
MAX_Q = 1e9
MAX_RATIO = 50.0
FALLBACK_COUNT = 5
HOURS = range(2, 25)


def _to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _anchor(case):
    known = case.get("known")
    if isinstance(known, list) and known and isinstance(known[0], dict):
        viewers = _to_float(known[0].get("viewers"))
        if viewers is not None:
            return max(1.0, viewers)
    return 1.0


def _release_curves(case):
    comparables = case.get("comparables")
    curves = []
    if not isinstance(comparables, list):
        return curves
    for comparable in comparables:
        if not isinstance(comparable, dict):
            continue
        launch = _parse_iso(comparable.get("day_start"))
        hourly = comparable.get("hourly")
        if launch is None or not isinstance(hourly, list):
            continue
        curve = [0.0] * 25
        for point in hourly:
            if not isinstance(point, dict):
                continue
            stamp = _parse_iso(point.get("from"))
            viewers = _to_float(point.get("viewers"))
            if stamp is None or viewers is None or viewers < 0:
                continue
            if stamp.date() != launch.date():
                continue
            hour = stamp.hour + 1
            if 1 <= hour <= 24:
                curve[hour] += viewers
        if 0.0 < curve[1] < math.inf:
            curves.append(curve)
    return curves


def _kernel_weight(anchor, opening):
    gap = math.log(anchor / opening)
    return math.exp(-(gap * gap) / (2.0 * KERNEL_WIDTH * KERNEL_WIDTH))


def _forecast_ratio(anchor, curves, hour):
    weighted_ratio = 0.0
    weight_sum = 0.0
    for curve in curves:
        ratio = curve[hour] / curve[1]
        if 0.0 < ratio < MAX_RATIO:
            weight = _kernel_weight(anchor, curve[1])
            weighted_ratio += weight * ratio
            weight_sum += weight
    if weight_sum >= MIN_WEIGHT_SUM:
        return weighted_ratio / weight_sum

    nearest = sorted(
        curves, key=lambda curve: abs(math.log(anchor / curve[1]))
    )[:FALLBACK_COUNT]
    ratios = []
    for curve in nearest:
        ratio = curve[hour] / curve[1]
        if 0.0 < ratio < MAX_RATIO:
            ratios.append(ratio)
    if ratios:
        return sum(ratios) / len(ratios)
    return 1.0


def _round2(value):
    return math.floor(value * 100.0 + 0.5) / 100.0


def _build_hours(anchor, curves):
    rows = []
    previous = None
    for hour in HOURS:
        center = anchor * _forecast_ratio(anchor, curves, hour)
        # sqrt(h-1): log-scale variance accumulates linearly with elapsed hours
        spread = SPREAD_PER_SQRT_HOUR * math.sqrt(hour - 1)
        quantiles = []
        for index, z in enumerate(Z5):
            value = center * math.exp(z * spread)
            value = min(max(value, MIN_Q), MAX_Q)
            value = _round2(value)
            if previous is not None and value < previous[index]:
                value = previous[index]
            quantiles.append(value)
        previous = quantiles
        rows.append({"hour": hour, "q": quantiles})
    return rows


def _output_name(case, stem):
    case_id = case.get("id")
    name = str(case_id) if case_id else stem
    name = Path(name).name
    if not name or name in (".", ".."):
        name = stem
    return name


def main():
    out_dir = Path(OUTPUT_DIR)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 1

    try:
        paths = sorted(Path(INPUT_DIR).glob("*.in"))
    except OSError:
        paths = []

    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                case = json.load(handle)
        except (OSError, ValueError):
            case = {}
        if not isinstance(case, dict):
            case = {}

        hours = _build_hours(_anchor(case), _release_curves(case))
        payload = json.dumps(
            {"hours": hours}, separators=(",", ":"), ensure_ascii=True
        ) + "\n"
        target = out_dir / (_output_name(case, path.stem) + ".out")
        try:
            target.write_text(payload, encoding="ascii")
        except OSError:
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
