#!/usr/bin/env python3
"""HealthDrop self-examination engine.

Reads the single canonical HealthDrop iCloud export and prints a privacy-safe,
sectioned health digest (or a machine-readable JSON with --json). This is the
computation engine for the `healthdrop` claw skill: the agent runs this
script and reads its printed digest instead of loading the ~1.9MB raw JSON into
context. All arithmetic happens here (deterministic Python), never on the raw
sample arrays in the model.

Design contract:
  * Standard library only (argparse, hashlib, json, math, os, sqlite3,
    statistics, sys, datetime, typing). The query mode keeps a derived SQLite
    index under ~/.cache/healthdrop so repeated reads don't re-parse the file.
  * One file in, one digest out. Exits 0 on success and even on no-data; exits
    nonzero ONLY when the file is missing or unparseable.
  * Privacy: emits aggregates/digests only -- never echoes raw SamplePoint or
    SleepInterval arrays, never the file body, never transmits anything.
  * Non-clinical: consumer wearable data, not a medical diagnosis. The strongest
    nudge anywhere is a soft "if this persists, consider a clinician".

Unit quirk (verified against real data): any metric whose unit string is "%" is
stored as a 0-1 FRACTION, not 0-100 (oxygenSaturation 0.97 -> 97%,
bodyFatPercentage 0.159 -> 15.9%). Multiply by 100 for display. SDNN is already
ms; vo2Max already ml/(kg*min); heart rates already bpm. No other conversions
except meters -> km for walking distance display.

Timezone assumptions:
  * generatedAt without an explicit offset is assumed UTC for age math.
  * All local-calendar-day bucketing (per-day sums, per-night attribution) uses
    the HOST machine's local timezone via the single shared local_day() helper,
    so day boundaries match what the user sees on their phone and are identical
    across every section.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import plistlib
import sqlite3
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

# Canonical iCloud Drive path the HealthDrop app overwrites on every export.
ICLOUD_INPUT = (
    "~/Library/Mobile Documents/iCloud~dev~keenranger~healthdrop/Documents/healthdrop.json"
)
# Local mirror landing zone (see `setup-mirror`). When this path exists and the
# caller did not pass an explicit override, the skill reads from here instead
# of iCloud -- the iCloud container is macOS-TCC-protected and not readable
# from non-FDA processes such as OpenClaw / Codex CLI launchers.
MIRROR_INPUT = "~/.healthdrop/healthdrop.json"
# Environment override that wins over both defaults. Documented escape hatch
# for users who want to point the skill at any path.
ENV_INPUT_OVERRIDE = "HEALTHDROP_EXPORT_PATH"
# Backwards-compatible alias: tests and the bundled HealthDrop app previously
# referenced DEFAULT_INPUT. Keep the symbol pointing at the canonical iCloud
# location so help text and any external references stay accurate.
DEFAULT_INPUT = ICLOUD_INPUT


def resolve_input(path: str, *, defaulted: bool = True) -> str:
    """Pick the actual file to open from the user's (or default) input path.

    Order of precedence:
      1. ``HEALTHDROP_EXPORT_PATH`` env var (escape hatch -- always wins).
      2. If the caller accepted the parser default AND a local mirror exists
         at MIRROR_INPUT, prefer the mirror. The iCloud container is
         TCC-protected on macOS; the mirror sits under a normal home-relative
         path that any user process can read. See the ``setup-mirror``
         subcommand for the agent / shell hook that maintains it.
      3. Otherwise: return the path as given (after ``~`` expansion).

    ``defaulted`` is the load-bearing distinction between "the parser
    silently filled in the iCloud path because the user passed nothing"
    (mirror auto-prefer applies) and "the user explicitly typed the
    canonical iCloud path because they want THAT file" (mirror is bypassed
    -- common after the user grants Full Disk Access and wants to confirm
    the source is fresher than a stale mirror).
    """
    env_override = os.environ.get(ENV_INPUT_OVERRIDE)
    if env_override:
        return os.path.expanduser(env_override)
    expanded = os.path.expanduser(path)
    if defaulted:
        mirror = os.path.expanduser(MIRROR_INPUT)
        if os.path.exists(mirror):
            return mirror
    return expanded

EXPECTED_SCHEMA_VERSION = 2

# Full expected metric catalog (key -> expected unit), mirroring QUANTITY_METRICS
# in src/health/metrics.ts. Used for the coverage/empty audit only.
METRIC_CATALOG: dict[str, str] = {
    "stepCount": "count",
    "activeEnergyBurned": "kcal",
    "basalEnergyBurned": "kcal",
    "appleExerciseTime": "min",
    "appleStandTime": "min",
    "flightsClimbed": "count",
    "distanceWalkingRunning": "m",
    "distanceCycling": "m",
    "distanceSwimming": "m",
    "heartRate": "count/min",
    "restingHeartRate": "count/min",
    "walkingHeartRateAverage": "count/min",
    "heartRateVariabilitySDNN": "ms",
    "oxygenSaturation": "%",
    "vo2Max": "ml/(kg*min)",
    "respiratoryRate": "count/min",
    "bodyMass": "kg",
    "bodyMassIndex": "count",
    "bodyFatPercentage": "%",
    "leanBodyMass": "kg",
    "appleSleepingWristTemperature": "degC",
    "bodyTemperature": "degC",
    "walkingSpeed": "m/s",
    "walkingStepLength": "cm",
    "walkingAsymmetryPercentage": "%",
    "walkingDoubleSupportPercentage": "%",
}

# Metrics whose samples accumulate within a day (sum per local day). Everything
# else in METRIC_CATALOG is instantaneous/sparse and is averaged, not summed.
CUMULATIVE_METRICS = {
    "stepCount",
    "activeEnergyBurned",
    "basalEnergyBurned",
    "appleExerciseTime",
    "appleStandTime",
    "flightsClimbed",
    "distanceWalkingRunning",
    "distanceCycling",
    "distanceSwimming",
}

# Numeric score per band, used to synthesize the recovery read-out.
_BAND_SCORE = {"green": 1, "amber": 0, "red": -1}

ASLEEP_STAGES = {"core", "deep", "rem", "asleepUnspecified"}  # stages that count as asleep


# --------------------------------------------------------------------------- #
# Shared parsing / time helpers
# --------------------------------------------------------------------------- #
def _normalize_iso(s: str) -> str:
    """Make an ISO 8601 string parseable by datetime.fromisoformat.

    Replaces a trailing 'Z' with '+00:00'. Tolerates fractional seconds.
    """
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s


def parse_dt(s: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp to a timezone-aware datetime.

    If the string carries no offset, assume UTC (documented assumption).
    Returns None on any failure so callers can degrade rather than crash.
    """
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(_normalize_iso(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_day(s: Any) -> Optional[date]:
    """Shared helper: LOCAL calendar day of an ISO timestamp.

    Converts to the host's local timezone, then takes the date. Every per-day
    sum and per-night attribution in this script funnels through here so day
    boundaries are consistent and match the user's phone.
    """
    dt = parse_dt(s)
    if dt is None:
        return None
    return dt.astimezone().date()  # astimezone() with no arg -> host local tz


def epoch(s: Any) -> Optional[float]:
    """Epoch seconds for an ISO timestamp, or None."""
    dt = parse_dt(s)
    return dt.timestamp() if dt else None


def fmt_local(s: Any) -> str:
    """Human-readable local-time rendering of an ISO timestamp for headers."""
    dt = parse_dt(s)
    if dt is None:
        return str(s)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def hm(seconds: float) -> str:
    """Format a duration in seconds as 'Hh Mm'."""
    total_min = int(round(seconds / 60.0))
    h, m = divmod(total_min, 60)
    return f"{h}h {m}m"


def safe_num(v: Any) -> Optional[float]:
    """Coerce to float if finite, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def values_of(samples: list[dict]) -> list[float]:
    """Extract finite numeric .value from a SamplePoint list."""
    out: list[float] = []
    for s in samples:
        v = safe_num(s.get("value")) if isinstance(s, dict) else None
        if v is not None:
            out.append(v)
    return out


def merge_intervals(intervals: list[tuple[float, float]]) -> float:
    """Union of covered seconds over [start, end) intervals (handles overlap)."""
    spans = sorted((a, b) for a, b in intervals if b > a)
    if not spans:
        return 0.0
    total = 0.0
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:  # overlapping / contiguous
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def as_dict(v: Any) -> dict:
    """Coerce to a dict, or {} if it isn't one (defensive against malformed JSON)."""
    return v if isinstance(v, dict) else {}


def as_list(v: Any) -> list:
    """Coerce to a list, or [] if it isn't one."""
    return v if isinstance(v, list) else []


def worst_band(*bands: str) -> str:
    """Worst of green/amber/red across the given bands ('na' ignored); returns
    'na' only when nothing is graded."""
    graded = [b for b in bands if b in ("red", "amber", "green")]
    if "red" in graded:
        return "red"
    if "amber" in graded:
        return "amber"
    return "green" if graded else "na"


# --------------------------------------------------------------------------- #
# Generic per-day-sum-then-average for cumulative metrics
# --------------------------------------------------------------------------- #
def daily_sum_then_average(samples: list[dict], expected_unit: str) -> Optional[dict]:
    """Sum samples per LOCAL day, then average daily totals across days.

    Used for cumulative metrics (steps, energy, exercise/stand minutes, flights,
    distance). NEVER averages raw per-sample values. Returns headline (all-days)
    and active-days averages plus coverage and a simple earlier-vs-recent trend.
    Adds a unit-mismatch note if any sample's unit differs from expected.
    """
    if not samples:
        return None
    per_day: dict[date, float] = {}
    unit_mismatch = False
    total = 0.0
    for s in samples:
        if not isinstance(s, dict):
            continue
        v = safe_num(s.get("value"))
        if v is None:
            continue
        u = s.get("unit")
        if expected_unit and u is not None and u != expected_unit:
            unit_mismatch = True
            continue  # skip schema-drifted sample
        d = local_day(s.get("startDate"))
        if d is None:
            continue
        per_day[d] = per_day.get(d, 0.0) + v
        total += v
    if not per_day:
        return None
    observed_days = len(per_day)
    days_sorted = sorted(per_day)
    daily_totals = [per_day[d] for d in days_sorted]

    # earlier vs recent halves (only meaningful with >=4 observed days)
    trend = None
    if observed_days >= 4:
        half = observed_days // 2
        earlier = daily_totals[:half]
        recent = daily_totals[observed_days - half:]
        if earlier and recent:
            trend = statistics.fmean(recent) - statistics.fmean(earlier)

    return {
        "total": total,
        "observed_days": observed_days,
        "active_days_avg": total / observed_days,
        "min_day": min(daily_totals),
        "max_day": max(daily_totals),
        "trend": trend,  # recent-minus-earlier daily total
        "unit_mismatch": unit_mismatch,
    }


# --------------------------------------------------------------------------- #
# SLEEP domain
# --------------------------------------------------------------------------- #
def analyze_sleep(sleep: list[dict]) -> dict:
    """Per-night sleep aggregation -> 7-night averages with bands and flags."""
    out: dict[str, Any] = {"status": "no_data", "band": "na", "flags": []}
    if not sleep:
        return out

    # Step 1: normalize intervals, drop zero/negative-duration ones.
    norm: list[dict] = []
    dropped_bad = 0
    for iv in sleep:
        if not isinstance(iv, dict):
            continue
        s = epoch(iv.get("startDate"))
        e = epoch(iv.get("endDate"))
        if s is None or e is None or e <= s:
            dropped_bad += 1
            continue
        norm.append(
            {
                "s": s,
                "e": e,
                "stage": iv.get("stage"),
                "source": iv.get("source"),
                "end_iso": iv.get("endDate"),
            }
        )
    if dropped_bad:
        out["flags"].append(("zero_or_negative_interval_dropped", dropped_bad))
    if not norm:
        return out

    # Step 2: assign each interval to a night by LOCAL date of its endDate.
    nights: dict[date, list[dict]] = {}
    for iv in norm:
        d = local_day(iv["end_iso"])
        if d is None:
            continue
        nights.setdefault(d, []).append(iv)
    if not nights:
        return out

    multi_source_nights = 0
    per_night: list[dict] = []

    for night_key in sorted(nights):
        ivs = nights[night_key]

        # Step 3: source dedup -- keep the single source with max asleep coverage.
        sources = {iv.get("source") for iv in ivs}
        kept_source = None
        if len([x for x in sources if x is not None]) > 1:
            multi_source_nights += 1
            best, best_cov = None, -1.0
            for src in sources:
                cov = sum(
                    iv["e"] - iv["s"]
                    for iv in ivs
                    if iv.get("source") == src and iv["stage"] in ASLEEP_STAGES
                )
                if cov > best_cov:
                    best, best_cov = src, cov
            kept_source = best
            # Keep the best asleep source's stages, but retain inBed intervals from
            # ANY source -- otherwise a companion source that only records in-bed time
            # is dropped, collapsing time-in-bed to the asleep envelope and inflating
            # efficiency (8h in-bed / 6h asleep would read as 100%).
            ivs = [iv for iv in ivs if iv.get("source") == kept_source or iv["stage"] == "inBed"]

        # Per-stage union-merged seconds (merge handles rare overlap).
        def stage_secs(stage: str) -> float:
            return merge_intervals([(iv["s"], iv["e"]) for iv in ivs if iv["stage"] == stage])

        core = stage_secs("core")
        deep = stage_secs("deep")
        rem = stage_secs("rem")
        unspec = stage_secs("asleepUnspecified")
        asleep = merge_intervals(
            [(iv["s"], iv["e"]) for iv in ivs if iv["stage"] in ASLEEP_STAGES]
        )

        inbed_ivs = [(iv["s"], iv["e"]) for iv in ivs if iv["stage"] == "inBed"]
        inbed_present = bool(inbed_ivs)
        if inbed_present:
            time_in_bed = merge_intervals(inbed_ivs)
        else:
            # envelope fallback: span of all non-inBed intervals (structurally optimistic)
            non_inbed = [(iv["s"], iv["e"]) for iv in ivs if iv["stage"] != "inBed"]
            time_in_bed = (max(e for _, e in non_inbed) - min(s for s, _ in non_inbed)) if non_inbed else 0.0

        if asleep <= 0:
            continue  # nothing to report for this night

        # Stage detail availability: suppress percentages if asleepUnspecified-dominated.
        staged = core + deep + rem
        stages_available = staged >= 0.5 * asleep

        efficiency = (asleep / time_in_bed * 100.0) if time_in_bed > 0 else None
        eff_over_100 = efficiency is not None and efficiency > 100.0
        if efficiency is not None:
            efficiency = min(efficiency, 100.0)

        # Awakenings = count of awake intervals within the sleep period (WASO).
        asleep_pts = [(iv["s"], iv["e"]) for iv in ivs if iv["stage"] in ASLEEP_STAGES]
        awakenings = 0
        waso = 0.0
        sol = None
        if asleep_pts:
            onset = min(s for s, _ in asleep_pts)
            offset = max(e for _, e in asleep_pts)
            for iv in ivs:
                if iv["stage"] == "awake" and iv["s"] >= onset and iv["e"] <= offset:
                    awakenings += 1
                    waso += iv["e"] - iv["s"]
            # Sleep onset latency: derivable only with an inBed marker at/before onset.
            if inbed_present:
                inbed_start = min(s for s, _ in inbed_ivs)
                if inbed_start <= onset:
                    sol = (onset - inbed_start) / 60.0
                    if sol < 0:
                        sol = None

        night = {
            "night": night_key.isoformat(),
            "asleep_sec": asleep,
            "in_bed_sec": time_in_bed,
            "efficiency": efficiency,
            "deep_pct": (deep / asleep * 100.0) if stages_available and asleep else None,
            "rem_pct": (rem / asleep * 100.0) if stages_available and asleep else None,
            "light_pct": (core / asleep * 100.0) if stages_available and asleep else None,
            "unspec_sec": unspec,
            "stages_available": stages_available,
            "awakenings": awakenings,
            "waso_min": waso / 60.0,
            "sol_min": sol,
            "inbed_present": inbed_present,
            "kept_source": kept_source,
            "onset_min_after_18": None,
            "wake_min_after_18": None,
            "eff_over_100": eff_over_100,
        }
        # clock-time anchors for consistency (avoid midnight wrap on onset)
        if asleep_pts:
            onset_dt = datetime.fromtimestamp(min(s for s, _ in asleep_pts)).astimezone()
            wake_dt = datetime.fromtimestamp(max(e for _, e in asleep_pts)).astimezone()
            night["onset_min_after_18"] = ((onset_dt.hour - 18) % 24) * 60 + onset_dt.minute
            night["wake_min_after_18"] = ((wake_dt.hour - 18) % 24) * 60 + wake_dt.minute
        per_night.append(night)

    if not per_night:
        return out

    # Implausible-night quarantine (naps / merged periods), excluded from averages.
    valid, quarantined = [], 0
    for n in per_night:
        if n["asleep_sec"] < 3 * 3600 or n["in_bed_sec"] < 3 * 3600 or n["in_bed_sec"] > 14 * 3600:
            quarantined += 1
        else:
            valid.append(n)
    if quarantined:
        out["flags"].append(("implausible_night_excluded", quarantined))
    used = valid if valid else per_night  # if all look odd, still report (flagged)
    n_nights = len(used)

    def mean_of(key: str, only_staged: bool = False) -> Optional[float]:
        vals = []
        for n in used:
            if only_staged and not n["stages_available"]:
                continue
            v = n[key]
            if v is None:
                continue
            vals.append(v)
        return statistics.fmean(vals) if vals else None

    tst_avg = statistics.fmean([n["asleep_sec"] for n in used])
    tib_avg = statistics.fmean([n["in_bed_sec"] for n in used])
    eff_avg = mean_of("efficiency")
    deep_avg = mean_of("deep_pct", only_staged=True)
    rem_avg = mean_of("rem_pct", only_staged=True)
    light_avg = mean_of("light_pct", only_staged=True)
    awk_avg = statistics.fmean([n["awakenings"] for n in used])
    waso_avg = statistics.fmean([n["waso_min"] for n in used])
    sol_avg = mean_of("sol_min")

    any_envelope = any(not n["inbed_present"] for n in used)
    all_envelope = all(not n["inbed_present"] for n in used)
    any_unstaged = any(not n["stages_available"] for n in used)

    # Consistency (needs >=3 nights).
    dur_sd = onset_sd = wake_sd = None
    if n_nights >= 3:
        dur_sd = statistics.pstdev([n["asleep_sec"] / 60.0 for n in used])
        onset_vals = [n["onset_min_after_18"] for n in used if n["onset_min_after_18"] is not None]
        wake_vals = [n["wake_min_after_18"] for n in used if n["wake_min_after_18"] is not None]
        if len(onset_vals) >= 3:
            onset_sd = statistics.pstdev(onset_vals)
        if len(wake_vals) >= 3:
            wake_sd = statistics.pstdev(wake_vals)

    # --- Bands (consumer green/amber/red), worst-of TST + efficiency -> verdict.
    def band_tst(h: float) -> str:
        if h < 6 or h > 9:
            return "red"
        if h < 7 or h > 8:
            return "amber"
        return "green"

    def band_eff(pct: Optional[float]) -> str:
        if pct is None:
            return "na"
        b = "green" if pct >= 85 else ("amber" if pct >= 75 else "red")
        # envelope fallback is optimistic: demote a red to amber
        if b == "red" and any_envelope:
            b = "amber"
        return b

    def band_deep(p: Optional[float]) -> str:
        if p is None:
            return "na"
        return "green" if p >= 15 else ("amber" if p >= 10 else "red")

    def band_rem(p: Optional[float]) -> str:
        if p is None:
            return "na"
        if 20 <= p <= 25:
            return "green"
        if 15 <= p < 20 or 25 < p <= 30:
            return "amber"
        return "red"

    tst_h = tst_avg / 3600.0
    b_tst = band_tst(tst_h)
    b_eff = band_eff(eff_avg)
    worst = worst_band(b_tst, b_eff)

    if n_nights < 3:
        out["flags"].append(("single_night_only", n_nights))
    if any_unstaged:
        out["flags"].append(("stages_unavailable", None))
    if any_envelope:
        out["flags"].append(("no_inbed_envelope_fallback", None))
    if sol_avg is None:
        out["flags"].append(("latency_not_derivable", None))
    if multi_source_nights:
        out["flags"].append(("multi_source_night", multi_source_nights))
    if any(n["eff_over_100"] for n in used):
        out["flags"].append(("efficiency_over_100", None))

    out.update(
        {
            "status": "ok",
            "band": worst,
            "n_nights": n_nights,
            "tst_h": tst_h,
            "tib_h": tib_avg / 3600.0,
            "efficiency": eff_avg,
            "deep_pct": deep_avg,
            "rem_pct": rem_avg,
            "light_pct": light_avg,
            "awakenings": awk_avg,
            "waso_min": waso_avg,
            "sol_min": sol_avg,
            "dur_sd_min": dur_sd,
            "onset_sd_min": onset_sd,
            "wake_sd_min": wake_sd,
            "stages_available": not any_unstaged,
            "envelope_fallback": any_envelope,
            "all_envelope": all_envelope,
            "b_tst": b_tst,
            "b_eff": b_eff,
            "b_deep": band_deep(deep_avg),
            "b_rem": band_rem(rem_avg),
            "headline_value": f"{hm(tst_avg)} asleep, {eff_avg:.0f}% eff"
            if eff_avg is not None
            else f"{hm(tst_avg)} asleep",
        }
    )
    return out


# --------------------------------------------------------------------------- #
# CARDIOVASCULAR & RECOVERY domain
# --------------------------------------------------------------------------- #
def _half_split_delta(samples: list[dict]) -> Optional[float]:
    """Chronological 2nd-half-minus-1st-half mean delta; needs n>=4 readings."""
    pairs = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        v = safe_num(s.get("value"))
        t = epoch(s.get("startDate"))
        if v is not None and t is not None:
            pairs.append((t, v))
    if len(pairs) < 4:
        return None
    pairs.sort(key=lambda p: p[0])
    vals = [v for _, v in pairs]
    half = len(vals) // 2  # symmetric split; drops the middle reading when n is odd
    return statistics.fmean(vals[-half:]) - statistics.fmean(vals[:half])


def analyze_cardio(metrics: dict, window_days: int) -> dict:
    """Resting HR, HRV, SpO2, respiratory rate, walking/overall HR -> recovery."""
    out: dict[str, Any] = {"status": "no_data", "band": "na", "flags": [], "signals": {}}
    short_window = window_days < 3

    # Resting HR (bpm, no conversion). Sparse: collect all, average.
    rhr = values_of(metrics.get("restingHeartRate") or [])
    resting = None
    if rhr:
        resting = {
            "avg": statistics.fmean(rhr),
            "min": min(rhr),
            "max": max(rhr),
            "count": len(rhr),
            "delta": _half_split_delta(metrics.get("restingHeartRate") or []),
        }

    # HRV SDNN (ms, no conversion). Prefer median for headline when n>=5.
    hrv_vals = values_of(metrics.get("heartRateVariabilitySDNN") or [])
    hrv = None
    if hrv_vals:
        hrv = {
            "avg": statistics.fmean(hrv_vals),
            "median": statistics.median(hrv_vals),
            "min": min(hrv_vals),
            "max": max(hrv_vals),
            "count": len(hrv_vals),
            "delta": _half_split_delta(metrics.get("heartRateVariabilitySDNN") or []),
        }

    # SpO2 (% stored as 0-1 fraction). Validate [0.5, 1.0] then *100. Min drives.
    spo2 = None
    raw_spo2 = metrics.get("oxygenSaturation") or []
    pcts, anomaly = [], 0
    for s in raw_spo2:
        v = safe_num(s.get("value")) if isinstance(s, dict) else None
        if v is None:
            continue
        if 0.5 <= v <= 1.0:
            pcts.append(v * 100.0)
        else:
            anomaly += 1
    if anomaly:
        out["flags"].append(("spo2_unit_anomaly", anomaly))
    if pcts:
        pcts_sorted = sorted(pcts)
        # 5th percentile (nearest-rank, stdlib only)
        idx = max(0, math.ceil(0.05 * len(pcts_sorted)) - 1)
        spo2 = {
            "avg": statistics.fmean(pcts),
            "min": min(pcts),
            "p5": pcts_sorted[idx],
            "low_frac": sum(1 for p in pcts if p < 90) / len(pcts),
            "count": len(pcts),
        }

    # Respiratory rate (breaths/min, no conversion).
    rr = values_of(metrics.get("respiratoryRate") or [])
    resp = None
    if rr:
        resp = {
            "avg": statistics.fmean(rr),
            "min": min(rr),
            "max": max(rr),
            "count": len(rr),
            "delta": _half_split_delta(metrics.get("respiratoryRate") or []),
        }

    # Walking HR (context only, never graded).
    whr = values_of(metrics.get("walkingHeartRateAverage") or [])
    walking = {"avg": statistics.fmean(whr), "count": len(whr)} if whr else None

    # Overall heart rate range (distinct from resting HR).
    hr = values_of(metrics.get("heartRate") or [])
    hr_overall = (
        {"min": min(hr), "avg": statistics.fmean(hr), "max": max(hr), "count": len(hr)}
        if hr
        else None
    )

    # --- Per-signal bands (green=+1, amber=0, red=-1). Mixed polarity.
    signals: dict[str, int] = {}
    bands: dict[str, str] = {}

    if resting and resting["count"] >= 3:
        a = resting["avg"]
        b = "green" if a < 65 else ("amber" if a < 80 else "red")
        # within-week rise overrides toward worse
        d = resting["delta"]
        if d is not None:
            if d > 6:
                b = "red"
            elif d >= 3 and b == "green":
                b = "amber"
        bands["restingHR"] = b
        signals["restingHR"] = _BAND_SCORE[b]
    elif resting:
        out["flags"].append(("sparse_metric", "restingHeartRate"))

    if hrv and hrv["count"] >= 3:
        med = hrv["median"]
        b = "green" if med >= 50 else ("amber" if med >= 30 else "red")
        d = hrv["delta"]
        if d is not None:
            if d < -8:
                b = "red"
            elif d < 0 and b == "green":
                b = "amber"
        bands["HRV"] = b
        signals["HRV"] = _BAND_SCORE[b]
    elif hrv:
        out["flags"].append(("sparse_metric", "heartRateVariabilitySDNN"))

    if spo2 and spo2["count"] >= 3:
        if spo2["min"] >= 95 and spo2["low_frac"] == 0:
            b = "green"
        elif spo2["min"] < 90 or spo2["low_frac"] >= 0.05:
            b = "red"
        else:
            b = "amber"
        bands["SpO2"] = b
        signals["SpO2"] = _BAND_SCORE[b]
        if b == "red":
            out["flags"].append(("spo2_low_dips", round(spo2["min"], 1)))

    if resp and resp["count"] >= 3 and resp["delta"] is not None:
        d = resp["delta"]
        b = "green" if d <= 1.0 else ("amber" if d <= 2.5 else "red")
        bands["respRate"] = b
        signals["respRate"] = _BAND_SCORE[b]
        if d > 2.5:
            out["flags"].append(("resp_rate_shift", round(d, 1)))

    if resting and resting.get("delta") is not None and resting["delta"] > 6:
        out["flags"].append(("resting_hr_rising", round(resting["delta"], 1)))
    if hrv and hrv.get("delta") is not None and hrv["delta"] < -8:
        out["flags"].append(("hrv_falling", round(hrv["delta"], 1)))

    # --- Recovery synthesis (need >=2 graded signals).
    recovery_band = "insufficient"
    recovery_score = None
    if len(signals) >= 2:
        recovery_score = statistics.fmean(list(signals.values()))
        if recovery_score >= 0.5:
            recovery_band = "green"
        elif recovery_score >= -0.25:
            recovery_band = "amber"
        else:
            recovery_band = "red"
        # conflicting signals -> avoid false confidence
        if "restingHR" in bands and "HRV" in bands:
            if {bands["restingHR"], bands["HRV"]} == {"green", "red"}:
                recovery_band = "amber"
                out["flags"].append(("conflicting_signals", None))
    else:
        out["flags"].append(("insufficient_recovery_inputs", len(signals)))
        # A lone red signal is still a real red driver -- surface it instead of
        # hiding it behind "insufficient" (a sparse but red metric still matters).
        if signals and min(signals.values()) < 0:
            recovery_band = "red"

    if short_window:
        out["flags"].append(("short_window", window_days))

    # Anything to report at all?
    if not any([resting, hrv, spo2, resp, walking, hr_overall]):
        return out

    headline_bits = []
    if resting:
        headline_bits.append(f"RHR {resting['avg']:.0f} bpm")
    if hrv:
        headline_bits.append(f"HRV {hrv['median']:.0f} ms")

    out.update(
        {
            "status": "ok",
            "band": recovery_band if recovery_band != "insufficient" else "na",
            "recovery_band": recovery_band,
            "recovery_score": recovery_score,
            "resting": resting,
            "hrv": hrv,
            "spo2": spo2,
            "resp": resp,
            "walking": walking,
            "hr_overall": hr_overall,
            "bands": bands,
            "signals": signals,
            "short_window": short_window,
            "headline_value": ", ".join(headline_bits) if headline_bits else "see details",
        }
    )
    return out


# --------------------------------------------------------------------------- #
# ACTIVITY & ENERGY domain
# --------------------------------------------------------------------------- #
def analyze_activity(metrics: dict, workouts: list[dict], window_days: int) -> dict:
    """Steps, energy, exercise/stand minutes, flights, distance, workouts."""
    out: dict[str, Any] = {"status": "no_data", "band": "na", "flags": []}
    any_metric = False

    def metric(key: str) -> Optional[dict]:
        nonlocal any_metric
        samples = metrics.get(key) or []
        r = daily_sum_then_average(samples, METRIC_CATALOG.get(key, ""))
        if r is not None:
            any_metric = True
            if r.get("unit_mismatch"):
                out["flags"].append(("unit_mismatch", key))
            r["headline_avg"] = r["total"] / max(1, window_days)
            r["coverage"] = min(1.0, r["observed_days"] / max(1, window_days))
        return r

    steps = metric("stepCount")
    active = metric("activeEnergyBurned")
    basal = metric("basalEnergyBurned")
    exercise = metric("appleExerciseTime")
    stand = metric("appleStandTime")
    flights = metric("flightsClimbed")
    walk = metric("distanceWalkingRunning")

    # Implausibly-high single-day guard (sensor/aggregation artifacts).
    if steps and steps["max_day"] > 60000:
        out["flags"].append(("implausible_high", "stepCount"))
    if active and active["max_day"] > 4000:
        out["flags"].append(("implausible_high", "activeEnergyBurned"))

    # Coverage gate keyed on steps.
    coverage = steps["coverage"] if steps else None
    if coverage is not None:
        if coverage < 0.5:
            out["flags"].append(("low_coverage", round(coverage, 2)))
        elif coverage < 0.85:
            out["flags"].append(("partial_coverage", round(coverage, 2)))

    if basal and steps and basal["observed_days"] < steps["observed_days"] - 1:
        out["flags"].append(("basal_backfill_gap", None))

    # --- Workout sanity check (junk vs plausible).
    plausible, junk = [], 0
    for w in workouts or []:
        if not isinstance(w, dict):
            continue
        dur = safe_num(w.get("durationSec"))
        kcal = safe_num(w.get("totalEnergyKcal"))
        dist = safe_num(w.get("totalDistanceMeters"))
        s = epoch(w.get("startDate"))
        e = epoch(w.get("endDate"))
        if (
            dur is None
            or dur <= 0
            or dur < 300
            or (s is not None and e is not None and e <= s)
            or (kcal is not None and kcal < 5)
            # A valid-duration session with no energy/distance (strength, yoga, or a
            # source that omits them) is still a real workout -- duration + dates carry it.
            or (kcal is not None and dur and kcal / (dur / 60.0) < 1.0 and (dist is None or dist < 100))
        ):
            junk += 1
            continue
        # Past the junk filter, dur is a positive float (>= 300s).
        plausible.append({"durMin": dur / 60.0, "kcal": kcal, "distM": dist})
    workout_summary = {
        "plausible_count": len(plausible),
        "junk_count": junk,
        "avg_dur_min": statistics.fmean([p["durMin"] for p in plausible]) if plausible else None,
        "total_kcal": sum(p["kcal"] for p in plausible if p["kcal"]) if plausible else 0,
        "total_dist_km": (
            sum(p["distM"] for p in plausible if p["distM"]) / 1000.0 if plausible else 0
        ),
    }
    if junk > 0 and len(plausible) == 0 and (workouts or []):
        out["flags"].append(("all_workouts_junk", junk))
    elif junk > 0:
        out["flags"].append(("junk_workouts", junk))

    if not any_metric and not (workouts or []):
        return out

    # --- Bands.
    def band_steps(v: float) -> str:
        return "green" if v >= 8000 else ("amber" if v >= 5000 else "red")

    def band_exercise_week(week_min: float) -> str:
        return "green" if week_min >= 150 else ("amber" if week_min >= 75 else "red")

    def band_active(v: float) -> str:
        return "green" if v >= 400 else ("amber" if v >= 200 else "red")

    b_steps = band_steps(steps["headline_avg"]) if steps else "na"
    weekly_ex = exercise["headline_avg"] * 7 if exercise else None
    b_ex = band_exercise_week(weekly_ex) if weekly_ex is not None else "na"
    b_active = band_active(active["headline_avg"]) if active else "na"

    if window_days == 1:
        out["flags"].append(("single_day_window", None))
        worst = "na"  # no verdict on a single noisy day
    else:
        worst = worst_band(b_steps, b_ex, b_active)

    headline = []
    if steps:
        headline.append(f"{int(round(steps['headline_avg'] / 10) * 10):,} steps/day")
    if exercise:
        headline.append(f"{int(round(exercise['headline_avg']))} exercise min/day")

    out.update(
        {
            "status": "ok",
            "band": worst,
            "window_days": window_days,
            "steps": steps,
            "active": active,
            "basal": basal,
            "exercise": exercise,
            "weekly_exercise_min": weekly_ex,
            "stand": stand,
            "flights": flights,
            "walk": walk,
            "workouts": workout_summary,
            "b_steps": b_steps,
            "b_exercise": b_ex,
            "b_active": b_active,
            "coverage": coverage,
            "headline_value": " · ".join(headline) if headline else "see details",
        }
    )
    return out


# --------------------------------------------------------------------------- #
# BODY COMPOSITION & GAIT domain
# --------------------------------------------------------------------------- #
def _latest(samples: list[dict]) -> Optional[dict]:
    """Most-recent reading by endDate; returns value/unit/asOf/n."""
    best, best_t, n = None, None, 0
    for s in samples:
        if not isinstance(s, dict):
            continue
        v = safe_num(s.get("value"))
        t = epoch(s.get("endDate"))
        if v is None or t is None:
            continue
        n += 1  # count only readings actually considered (valid value AND endDate)
        if best_t is None or t > best_t:
            best, best_t = s, t
    if best is None:
        return None
    return {
        "value": safe_num(best.get("value")),
        "unit": best.get("unit"),
        "as_of": best.get("endDate"),
        "n": n,
    }


def _gait_daily_then_window(samples: list[dict], is_pct: bool) -> Optional[dict]:
    """Per-LOCAL-day MEAN of values (rates/ratios -> never sum), then mean of
    daily means across the window. Converts fraction->percent if is_pct."""
    per_day: dict[date, list[float]] = {}
    anomaly = False
    for s in samples:
        if not isinstance(s, dict):
            continue
        v = safe_num(s.get("value"))
        if v is None:
            continue
        if is_pct and v > 1.0:  # defensive: stored as whole-percent already
            anomaly = True
            continue
        d = local_day(s.get("startDate"))
        if d is None:
            continue
        per_day.setdefault(d, []).append(v)
    if not per_day:
        return None
    day_means = [statistics.fmean(vs) for vs in per_day.values()]
    mult = 100.0 if is_pct else 1.0
    return {
        "avg": statistics.fmean(day_means) * mult,
        "min_day": min(day_means) * mult,
        "max_day": max(day_means) * mult,
        "day_count": len(per_day),
        "sample_count": sum(len(vs) for vs in per_day.values()),
        "anomaly": anomaly,
    }


def analyze_body_gait(metrics: dict, window_days: int) -> dict:
    """Body composition (latest snapshot) + gait (per-day-mean then window-avg)."""
    out: dict[str, Any] = {"status": "no_data", "band": "na", "flags": [], "gait_band": "na"}

    # --- Body composition: latest reading, never averaged.
    body_mass = _latest(metrics.get("bodyMass") or [])
    bmi = _latest(metrics.get("bodyMassIndex") or [])
    lean = _latest(metrics.get("leanBodyMass") or [])
    bf_raw = _latest(metrics.get("bodyFatPercentage") or [])
    body_fat = None
    if bf_raw and bf_raw["value"] is not None:
        v = bf_raw["value"]
        if v > 1.0:  # defensive: already whole-percent
            out["flags"].append(("bodyfat_unit_anomaly", v))
            body_fat = {**bf_raw, "pct": v}
        else:
            body_fat = {**bf_raw, "pct": v * 100.0}

    has_body = any([body_mass, bmi, lean, body_fat])

    # BMI band (WHO general adult, informational).
    b_bmi = "na"
    if bmi and bmi["value"] is not None:
        x = bmi["value"]
        if x >= 30 or x < 17:
            b_bmi = "red"
        elif x >= 25 or x < 18.5:
            b_bmi = "amber"
        else:
            b_bmi = "green"
        if x < 8 or x > 90:
            out["flags"].append(("implausible_metric_value", "bodyMassIndex"))
            b_bmi = "na"

    # BMI vs body-fat mismatch (athlete/muscle caveat).
    if b_bmi != "na" and body_fat and bmi:
        bmi_v = bmi["value"]
        bf_pct = body_fat["pct"]
        if bmi_v is not None and bf_pct is not None:
            if bmi_v < 25 and bf_pct >= 30:
                out["flags"].append(("bmi_bodyfat_inconsistent", None))
            elif bmi_v >= 30 and bf_pct < 15:
                out["flags"].append(("bmi_bodyfat_inconsistent", None))

    if any(x and x.get("n") == 1 for x in (body_mass, bmi, lean, body_fat)):
        out["flags"].append(("single_reading_no_trend", None))

    # --- Gait: per-day-mean then window-average.
    speed = _gait_daily_then_window(metrics.get("walkingSpeed") or [], is_pct=False)
    step_len = _gait_daily_then_window(metrics.get("walkingStepLength") or [], is_pct=False)
    asym = _gait_daily_then_window(metrics.get("walkingAsymmetryPercentage") or [], is_pct=True)
    dbl = _gait_daily_then_window(metrics.get("walkingDoubleSupportPercentage") or [], is_pct=True)

    for label, g in (("walkingAsymmetryPercentage", asym), ("walkingDoubleSupportPercentage", dbl)):
        if g and g.get("anomaly"):
            out["flags"].append(("bodyfat_unit_anomaly", label))

    # Implausible-value drops (defensive bounds).
    if speed and (speed["avg"] <= 0 or speed["avg"] > 3.0):
        out["flags"].append(("implausible_metric_value", "walkingSpeed"))
        speed = None
    if step_len and (step_len["avg"] <= 0 or step_len["avg"] > 120):
        out["flags"].append(("implausible_metric_value", "walkingStepLength"))
        step_len = None

    has_gait = any([speed, step_len, asym, dbl])

    # Coverage / low-confidence per gait metric.
    def low_conf(g: Optional[dict]) -> bool:
        return bool(g and (g["day_count"] / max(1, window_days)) < 0.4)

    if has_gait and all(low_conf(g) for g in (speed, asym, dbl) if g):
        out["flags"].append(("gait_low_coverage", None))

    # Gait bands (sustained-average orientation).
    def band_speed(v: float) -> str:
        return "green" if v >= 1.0 else ("amber" if v >= 0.8 else "red")

    def band_asym(v: float) -> str:
        return "green" if v < 3 else ("amber" if v <= 5 else "red")

    def band_dbl(v: float) -> str:
        if 20 <= v <= 28:
            return "green"
        if (28 < v <= 34) or v < 18:
            return "amber"
        return "red"

    b_speed = band_speed(speed["avg"]) if speed else "na"
    b_asym = band_asym(asym["avg"]) if asym else "na"
    b_dbl = band_dbl(dbl["avg"]) if dbl else "na"

    gait_band = worst_band(b_speed, b_asym, b_dbl)

    # Only clinical-adjacent escalation: speed red AND (asym red OR dbl red).
    if b_speed == "red" and ("red" in (b_asym, b_dbl)):
        out["flags"].append(("gait_multi_red", None))

    if not has_body and not has_gait:
        return out

    headline = []
    if body_mass and body_mass["value"] is not None:
        headline.append(f"{body_mass['value']:.1f} kg")
    if speed:
        headline.append(f"gait {speed['avg']:.2f} m/s")

    out.update(
        {
            "status": "single_reading" if (has_body and not has_gait) else "ok",
            "band": b_bmi,  # body block band for synthesis (BMI is the only judged body metric)
            "gait_band": gait_band,
            "body_mass": body_mass,
            "bmi": bmi,
            "lean": lean,
            "body_fat": body_fat,
            "b_bmi": b_bmi,
            "speed": speed,
            "step_len": step_len,
            "asym": asym,
            "dbl": dbl,
            "b_speed": b_speed,
            "b_asym": b_asym,
            "b_dbl": b_dbl,
            "headline_value": " · ".join(headline) if headline else "see details",
        }
    )
    return out


# --------------------------------------------------------------------------- #
# Top-level assembly
# --------------------------------------------------------------------------- #
def build_report(data: dict) -> dict:
    """Compute the full digest from a parsed HealthSnapshot dict."""
    meta: dict[str, Any] = {"status": "ok"}
    flags: list[dict] = []

    # Schema gate (best-effort even on mismatch).
    schema_version = data.get("schemaVersion", 1)
    meta["schema_version"] = schema_version
    meta["schema_ok"] = schema_version == EXPECTED_SCHEMA_VERSION
    if not meta["schema_ok"]:
        flags.append(_flag("schema_mismatch", "info"))

    metrics = as_dict(data.get("metrics"))
    sleep = as_list(data.get("sleep"))
    workouts = as_list(data.get("workouts"))

    # Freshness + window.
    generated_at = data.get("generatedAt")
    gen_dt = parse_dt(generated_at)
    now = datetime.now(timezone.utc)
    age_hours = None
    staleness = "unknown"
    if gen_dt is not None:
        age_hours = round((now - gen_dt.astimezone(timezone.utc)).total_seconds() / 3600.0, 1)
        if age_hours < 0:
            staleness = "clock_skew"
            flags.append(_flag("clock_skew", "info"))
            age_hours = 0.0
        elif age_hours <= 24:
            staleness = "fresh"
        elif age_hours <= 72:
            staleness = "stale"
            flags.append(_flag("stale_data", "caution"))
        else:
            staleness = "very_stale"
            flags.append(_flag("very_stale_data", "caution"))

    rs = data.get("rangeStart")
    re = data.get("rangeEnd")
    d0, d1 = local_day(rs), local_day(re)
    rs_dt, re_dt = parse_dt(rs), parse_dt(re)
    if rs_dt and re_dt and re_dt > rs_dt:
        # Number of 24h periods in [rangeStart, rangeEnd] -- the real per-day
        # divisor. Inclusive calendar-date counting over-counts a normal 7x24h
        # export that straddles midnight as 8 days, under-reporting dailies.
        window_days = max(1, round((re_dt - rs_dt).total_seconds() / 86400.0))
    else:
        window_days = 1
    # stale_range: export ran but covers an old window.
    if gen_dt and re:
        re_dt = parse_dt(re)
        if re_dt and (gen_dt - re_dt).total_seconds() > 26 * 3600:
            flags.append(_flag("stale_range", "info"))
    # inverted / future range sanity.
    if d0 and d1 and d1 < d0:
        flags.append(_flag("future_or_inverted_range", "info"))

    meta.update(
        {
            "generated_at": generated_at,
            "generated_at_local": fmt_local(generated_at),
            "age_hours": age_hours,
            "staleness": staleness,
            "window_days": window_days,
            "range_start_local": d0.isoformat() if d0 else None,
            "range_end_local": d1.isoformat() if d1 else None,
        }
    )
    if window_days < 3:
        flags.append(_flag("partial_window", "caution"))

    # Coverage / empty audit.
    present, empty, absent = [], [], []
    for key in METRIC_CATALOG:
        if key not in metrics:
            absent.append(key)
        elif isinstance(metrics[key], list) and len(metrics[key]) > 0:
            present.append(key)
        else:
            empty.append(key)
    meta["coverage"] = {"present": present, "empty": empty, "absent": absent}

    everything_empty = not present and not sleep and not workouts
    if everything_empty:
        flags.append(_flag("all_empty", "caution"))

    # --- Domain analyses.
    domains = {
        "sleep": analyze_sleep(sleep),
        "recovery": analyze_cardio(metrics, window_days),
        "activity": analyze_activity(metrics, workouts, window_days),
        "body": analyze_body_gait(metrics, window_days),
    }

    # Bubble up domain-level data flags into the report-level Flags section.
    for dom_name, dom in domains.items():
        for f in dom.get("flags", []):
            fid, detail = f if isinstance(f, tuple) else (f, None)
            sev = "caution" if fid in _CAUTION_FLAGS else "info"
            flags.append(_flag(fid, sev, scope=dom_name, detail=detail))

    # Workout sanity flag at report level.
    wk = domains["activity"].get("workouts") or {}
    if wk.get("junk_count", 0) > 0:
        flags.append(_flag("workout_data_suspect", "info", detail=wk.get("junk_count")))

    # Single-reading-only report.
    only_single = (
        domains["body"].get("status") == "single_reading"
        and domains["sleep"]["status"] != "ok"
        and domains["recovery"]["status"] != "ok"
        and domains["activity"]["status"] != "ok"
    )
    if only_single:
        flags.append(_flag("single_reading_only", "info"))

    # --- Overall condition synthesis (worst-of over synthesis domains).
    synth_bands = []
    for name in ("recovery", "sleep", "activity"):
        b = domains[name].get("band", "na")
        if b in ("green", "amber", "red"):
            synth_bands.append((name, b))
    # Gait is a sustained signal (not a single snapshot), so a red gait belongs in
    # the top-line rollup; body composition / BMI deliberately stays out.
    gait_b = domains["body"].get("gait_band", "na")
    if gait_b in ("green", "amber", "red"):
        synth_bands.append(("gait", gait_b))
    n_avail = len(synth_bands)
    reds = [n for n, b in synth_bands if b == "red"]
    ambers = [n for n, b in synth_bands if b == "amber"]
    if n_avail == 0:
        overall = "insufficient_data"
    elif reds:
        overall = "attention"
    elif len(ambers) >= 2:
        overall = "mixed"
    elif len(ambers) == 1:
        overall = "watch"
    else:
        overall = "good"
    drivers = [(n, "red") for n in reds] + [(n, "amber") for n in ambers]
    data_completeness = n_avail / 4.0

    # Sort flags: caution before info, then by id.
    flags.sort(key=lambda f: (0 if f["severity"] == "caution" else 1, f["id"]))

    return {
        "schema": "healthdrop.digest/1",
        "meta": meta,
        "overall": {
            "condition": overall,
            "drivers": drivers,
            "data_completeness": round(data_completeness, 2),
        },
        "flags": flags,
        "domains": domains,
    }


# Flags whose default severity is "caution" (everything else -> info).
_CAUTION_FLAGS = {
    "low_coverage",
    "all_workouts_junk",
    "spo2_low_dips",
    "resting_hr_rising",
    "hrv_falling",
    "resp_rate_shift",
    "gait_multi_red",
    "short_window",
    "single_night_only",
}

# Bilingual flag messages.
_FLAG_MESSAGES: dict[str, tuple[str, str]] = {
    "no_file": (
        "정규 healthdrop.json 파일을 찾을 수 없어요 — iCloud 동기화 전이거나 내보내기를 안 했을 수 있어요.",
        "Canonical healthdrop.json not found — iCloud may not have synced or no export has run.",
    ),
    "parse_error": (
        "파일은 있지만 JSON으로 읽을 수 없어요.",
        "File present but not valid JSON.",
    ),
    "permission_denied": (
        "파일은 있지만 권한이 없어 읽지 못했어요 — macOS TCC 제한일 수 있어요. `setup-mirror` 또는 HEALTHDROP_EXPORT_PATH로 우회하세요.",
        "File exists but is not readable -- likely a macOS TCC restriction. Use `setup-mirror` or set HEALTHDROP_EXPORT_PATH.",
    ),
    "schema_mismatch": (
        "schemaVersion가 2가 아니에요 — 최선으로 파싱했지만 결과가 불완전할 수 있어요.",
        "schemaVersion is not 2 — parsed best-effort, results may be incomplete.",
    ),
    "stale_data": (
        "데이터가 24~72시간 전 기준이에요 — 신선도 주의.",
        "Data is 24-72h old — freshness caveat applies.",
    ),
    "very_stale_data": (
        "데이터가 72시간 이상 지났어요 — 추세를 믿기 전에 다시 내보내세요.",
        "Data is over 72h old — re-export before trusting trends.",
    ),
    "clock_skew": (
        "generatedAt가 현재 시각보다 미래예요 — 기기/Mac 시간 불일치 가능.",
        "generatedAt is in the future — possible device/Mac clock mismatch.",
    ),
    "stale_range": (
        "내보내기는 실행됐지만 오래된 기간을 담고 있어요.",
        "Export ran but covers an old window.",
    ),
    "partial_window": (
        "관측 기간이 3일 미만이라 추세·평균 표현은 신뢰도가 낮아요.",
        "Window under 3 days — trend/average language is unreliable.",
    ),
    "workout_data_suspect": (
        "비정상적으로 짧거나 칼로리가 낮은 운동 기록을 활동 집계에서 제외했어요.",
        "Excluded implausibly short / low-energy workout(s) from activity.",
    ),
    "all_empty": (
        "파일은 있지만 모든 지표·수면·운동이 비어 있어요 — 권한/내보내기 범위 문제일 수 있어요.",
        "File present but every metric, sleep, and workout is empty — likely a permissions/scope issue.",
    ),
    "single_reading_only": (
        "단일 측정값만 있어 추세를 낼 수 없어요.",
        "Only single-snapshot data available — no trends possible.",
    ),
    # sleep
    "single_night_only": ("기록된 밤이 적어 노이즈가 큽니다 (추세 아님).", "Few nights recorded — noisy, not a trend."),
    "stages_unavailable": (
        "일부 밤은 단계 정보가 없어 단계 비율을 생략했어요 (구형 기기/3rd-party).",
        "Some nights lack stage detail — stage percentages suppressed.",
    ),
    "no_inbed_envelope_fallback": (
        "inBed 기록이 없는 밤은 효율을 수면구간으로 추정했어요 (다소 낙관적).",
        "Nights without an inBed record use sleep-period-envelope efficiency (optimistic).",
    ),
    "latency_not_derivable": (
        "inBed 마커가 없어 잠들기까지 시간은 산출할 수 없어요.",
        "No in-bed marker — sleep latency can't be derived.",
    ),
    "multi_source_night": (
        "여러 소스가 겹친 밤은 커버리지가 가장 큰 한 소스만 사용했어요 (중복 합산 방지).",
        "Multi-source night(s) — kept the best-coverage source to avoid double-counting.",
    ),
    "implausible_night_excluded": (
        "너무 짧거나 긴 밤(낮잠/병합 기록)은 평균에서 제외했어요.",
        "Implausibly short/long night(s) excluded from averages.",
    ),
    "zero_or_negative_interval_dropped": (
        "길이가 0 이하인 수면 구간을 버렸어요.",
        "Dropped zero/negative-duration sleep interval(s).",
    ),
    "efficiency_over_100": (
        "계산된 효율이 100%를 넘어 100%로 보정했어요 (데이터 품질).",
        "Computed efficiency exceeded 100% — clamped (data quality).",
    ),
    # recovery
    "short_window": (
        "관측 기간이 짧아 추세 수치는 잠정값이에요.",
        "Short window — trend figures are provisional.",
    ),
    "spo2_unit_anomaly": (
        "혈중 산소 일부 값이 0-1 분수 범위를 벗어나 제외했어요.",
        "Some SpO2 values were outside the 0-1 fraction range — dropped.",
    ),
    "resting_hr_rising": (
        "주중 안정 심박이 뚜렷이 올랐어요 — 회복 주의 신호.",
        "Resting HR rose notably across the week — a recovery watch signal.",
    ),
    "hrv_falling": (
        "주중 HRV가 떨어졌어요 — 피로 누적 신호.",
        "HRV fell across the week — accumulating-fatigue signal.",
    ),
    "spo2_low_dips": (
        "혈중 산소가 반복적으로 낮게 떨어졌어요 — 지속되면 전문가와 상의하세요 (진단 아님).",
        "Repeated low blood-oxygen dips — if persistent, consider a clinician (not a diagnosis).",
    ),
    "resp_rate_shift": (
        "호흡수가 평소보다 올라갔어요 — 컨디션/질병과 함께 나타날 수 있어요.",
        "Respiratory rate shifted up vs baseline — can accompany illness/strain.",
    ),
    "sparse_metric": (
        "측정 횟수가 적어 이 지표는 잠정값으로 처리했어요.",
        "Too few readings — treated this metric as provisional.",
    ),
    "insufficient_recovery_inputs": (
        "지표가 부족해 회복 종합 판단을 낼 수 없어요.",
        "Not enough signals to compute a recovery read-out.",
    ),
    "conflicting_signals": (
        "안정 심박과 HRV 신호가 엇갈려 종합은 보통으로 두었어요.",
        "Resting HR and HRV disagree — overall held at amber.",
    ),
    # activity
    "low_coverage": (
        "착용일이 절반 미만이라 평균이 부정확해요 — 착용일 평균으로 표시했어요.",
        "Watch worn under half the days — averages unreliable; using active-day framing.",
    ),
    "partial_coverage": (
        "일부 날은 데이터가 없어요 — 착용일 평균을 함께 보세요.",
        "Some days have no data — see the active-day average too.",
    ),
    "junk_workouts": (
        "짧거나 비정상인 운동 기록은 무시했어요.",
        "Ignored short/implausible workout entries.",
    ),
    "all_workouts_junk": (
        "기록된 운동이 모두 비정상이라 실제 운동으로 보기 어려워요 — 걸음수/운동시간으로 판단하세요.",
        "All logged workouts look implausible — judge activity from steps + exercise minutes.",
    ),
    "unit_mismatch": (
        "카탈로그와 단위가 다른 샘플을 건너뛰었어요 (스키마 드리프트 가능).",
        "Skipped sample(s) whose unit differs from the catalog (possible schema drift).",
    ),
    "single_day_window": (
        "단 하루치라 추세 판단 없이 스냅샷으로만 봅니다.",
        "Single day only — reported as a snapshot, no verdict.",
    ),
    "implausible_high": (
        "하루치 값이 비현실적으로 높아 평균에서 의심 처리했어요.",
        "A single-day total looked implausibly high — flagged.",
    ),
    "basal_backfill_gap": (
        "기초대사 추정이 일부 날 누락됐어요.",
        "Basal estimate missing on some days.",
    ),
    # body / gait
    "single_reading_no_trend": (
        "단일 측정값이라 추세를 낼 수 없어요.",
        "Single reading — no trend possible.",
    ),
    "bodyfat_unit_anomaly": (
        "퍼센트 지표 값이 예상(0-1)을 벗어나 변환을 건너뛰었어요.",
        "A percent metric value was outside 0-1 — skipped the *100 conversion.",
    ),
    "bmi_bodyfat_inconsistent": (
        "BMI와 체지방률이 엇갈려요 (근육량 영향 가능) — BMI만으로 판단하지 않을게요.",
        "BMI and body-fat disagree (muscle-mass caveat) — not judging from BMI alone.",
    ),
    "gait_low_coverage": (
        "보행 데이터가 적어 평균 신뢰도가 낮아요.",
        "Limited walking data — low-confidence average.",
    ),
    "gait_multi_red": (
        "보행 속도와 더불어 비대칭/양발지지 중 하나 이상이 함께 기준을 벗어났어요 — 지속되면 전문가와 상의하세요.",
        "Walking speed plus asymmetry and/or double-support out of range together — if it persists, consider a clinician.",
    ),
    "implausible_metric_value": (
        "센서/소스 오류로 보이는 값을 제외했어요.",
        "Dropped a value that looked like sensor/source error.",
    ),
    "future_or_inverted_range": (
        "rangeEnd가 rangeStart보다 빠르거나 미래 표본이 있어요 — 기간 의심.",
        "rangeEnd before rangeStart or future-dated samples — window suspect.",
    ),
}


def _flag(fid: str, severity: str, scope: str = "report", detail: Any = None) -> dict:
    ko, en = _FLAG_MESSAGES.get(fid, (fid, fid))
    return {"id": fid, "severity": severity, "scope": scope, "detail": detail, "message_ko": ko, "message_en": en}


# --------------------------------------------------------------------------- #
# Human-readable rendering (English digest; bilingual strings live in --json)
# --------------------------------------------------------------------------- #
def _line(label: str, value: str) -> str:
    return f"  {label:<22}{value}"


def render_text(report: dict) -> str:
    m = report["meta"]
    L: list[str] = []
    L.append("=" * 64)
    L.append("HealthDrop self-examination")
    L.append("=" * 64)

    # Header
    badge = ""
    if m["staleness"] == "stale":
        badge = f"  [STALE: data {m['age_hours']}h old]"
    elif m["staleness"] == "very_stale":
        badge = f"  [VERY STALE: {m['age_hours']}h old — re-export recommended]"
    elif m["staleness"] == "clock_skew":
        badge = "  [clock skew: generatedAt in the future]"
    L.append(f"As of {m['generated_at_local']} · last {m['window_days']} day(s){badge}")
    if m["range_start_local"] and m["range_end_local"]:
        L.append(f"Window: {m['range_start_local']} .. {m['range_end_local']}")
    if not m["schema_ok"]:
        L.append(f"(schemaVersion={m['schema_version']}, expected 2 — best-effort parse)")

    # Overall
    o = report["overall"]
    verdict_map = {
        "good": "GOOD — no red flags",
        "watch": "WATCH — 1 caution signal",
        "mixed": "MIXED — multiple caution signals",
        "attention": "NEEDS ATTENTION",
        "insufficient_data": "INSUFFICIENT DATA",
    }
    L.append("")
    L.append(f"Overall condition: {verdict_map.get(o['condition'], o['condition'])}")
    if o["drivers"]:
        drv = ", ".join(f"{n} ({b})" for n, b in o["drivers"])
        L.append(f"  drivers: {drv}")
    L.append(f"  data completeness: {int(o['data_completeness'] * 100)}% of synthesis domains")

    dom = report["domains"]

    # Sleep
    L.append("")
    L.append("-- Sleep " + "-" * 55)
    s = dom["sleep"]
    if s["status"] != "ok":
        L.append("  no sleep data in window")
    else:
        L.append(_line("Time asleep (avg):", f"{hm(s['tst_h'] * 3600)} over {s['n_nights']} night(s)"))
        L.append(_line("Time in bed (avg):", hm(s["tib_h"] * 3600)))
        if s["efficiency"] is not None:
            if s.get("all_envelope"):
                note = " (envelope est., optimistic)"
            elif s["envelope_fallback"]:
                note = " (partly envelope est.)"
            else:
                note = ""
            L.append(_line("Efficiency:", f"{s['efficiency']:.0f}% [{s['b_eff']}]{note}"))
        if s["stages_available"] and s["deep_pct"] is not None:
            L.append(
                _line(
                    "Stages:",
                    f"Deep {s['deep_pct']:.0f}% [{s['b_deep']}] / "
                    f"REM {s['rem_pct']:.0f}% [{s['b_rem']}] / Light(core) {s['light_pct']:.0f}%",
                )
            )
        else:
            L.append(_line("Stages:", "not available (device didn't record stage detail)"))
        L.append(_line("Awakenings:", f"{s['awakenings']:.0f}/night · WASO {s['waso_min']:.0f} min"))
        if s["sol_min"] is not None:
            L.append(_line("Sleep latency:", f"~{s['sol_min']:.0f} min"))
        else:
            L.append(_line("Sleep latency:", "not derivable (no in-bed marker)"))
        if s["dur_sd_min"] is not None:
            L.append(_line("Duration consistency:", f"±{s['dur_sd_min']:.0f} min SD"))

    # Cardiovascular & recovery
    L.append("")
    L.append("-- Cardiovascular & recovery " + "-" * 34)
    c = dom["recovery"]
    if c["status"] != "ok":
        L.append("  no cardiovascular data in window")
    else:
        rb = c["recovery_band"] or "insufficient"
        rb_label = {"green": "good", "amber": "moderate", "red": "watch", "insufficient": "insufficient data"}.get(rb, rb)
        L.append(_line("Recovery read-out:", rb_label))

        def band_tag(k: str) -> str:
            return f" [{c['bands'][k]}]" if k in c["bands"] else ""

        if c["resting"]:
            r = c["resting"]
            dphr = f", trend {r['delta']:+.0f} bpm" if r["delta"] is not None else ""
            tag = band_tag("restingHR")
            L.append(_line("Resting HR:", f"{r['avg']:.0f} bpm (range {r['min']:.0f}-{r['max']:.0f}, n={r['count']}){dphr}{tag}"))
        if c["hrv"]:
            h = c["hrv"]
            dh = f", trend {h['delta']:+.0f} ms" if h["delta"] is not None else ""
            tag = band_tag("HRV")
            L.append(_line("HRV (SDNN):", f"median {h['median']:.0f} ms (mean {h['avg']:.0f}, n={h['count']}){dh}{tag}"))
        if c["spo2"]:
            sp = c["spo2"]
            tag = band_tag("SpO2")
            L.append(_line("Blood oxygen:", f"avg {sp['avg']:.1f}%, low {sp['min']:.1f}% (n={sp['count']}){tag}"))
        if c["resp"]:
            rr = c["resp"]
            dd = f", trend {rr['delta']:+.1f}" if rr["delta"] is not None else ""
            tag = band_tag("respRate")
            L.append(_line("Respiratory rate:", f"avg {rr['avg']:.1f} br/min (range {rr['min']:.0f}-{rr['max']:.0f}){dd}{tag}"))
        if c["hr_overall"]:
            ho = c["hr_overall"]
            L.append(_line("Heart rate range:", f"{ho['min']:.0f}-{ho['max']:.0f} bpm, avg {ho['avg']:.0f} (≠ resting HR)"))
        if c["walking"]:
            L.append(_line("Walking HR:", f"{c['walking']['avg']:.0f} bpm (context only)"))

    # Activity & energy
    L.append("")
    L.append("-- Activity & energy " + "-" * 42)
    a = dom["activity"]
    if a["status"] != "ok":
        L.append("  no activity data in window")
    else:
        def cov_note(metric_obj: Optional[dict]) -> str:
            if not metric_obj:
                return ""
            cov = metric_obj.get("coverage")
            if cov is not None and cov < 0.85:
                return f"  (active-day avg {metric_obj['active_days_avg']:,.0f}, {metric_obj['observed_days']}/{a['window_days']} days)"
            return ""
        if a["steps"]:
            st = a["steps"]
            L.append(_line("Steps:", f"{st['headline_avg']:,.0f}/day [{a['b_steps']}]{cov_note(st)}"))
        if a["exercise"]:
            ex = a["exercise"]
            wk = a["weekly_exercise_min"]
            pct = int(round(min(wk, 150) / 150 * 100)) if wk else 0
            meets = "meets" if wk and wk >= 150 else f"{pct}% of"
            L.append(_line("Exercise:", f"{ex['headline_avg']:.0f} min/day (~{wk:.0f}/wk, {meets} 150/wk) [{a['b_exercise']}]"))
        if a["active"]:
            L.append(_line("Active energy:", f"{a['active']['headline_avg']:.0f} kcal/day [{a['b_active']}]"))
        if a["basal"]:
            L.append(_line("Basal energy:", f"{a['basal']['headline_avg']:.0f} kcal/day (separate)"))
        bits = []
        if a["stand"]:
            bits.append(f"stand {a['stand']['headline_avg']:.0f} min/day")
        if a["flights"]:
            bits.append(f"{a['flights']['headline_avg']:.1f} flights/day")
        if a["walk"]:
            bits.append(f"{a['walk']['headline_avg'] / 1000:.1f} km walked/day")
        if bits:
            L.append(_line("Also:", " · ".join(bits)))
        wkt = a["workouts"]
        if wkt["plausible_count"] > 0:
            extra = f"; ignored {wkt['junk_count']} implausible" if wkt["junk_count"] else ""
            L.append(_line("Workouts:", f"{wkt['plausible_count']} real (avg {wkt['avg_dur_min']:.0f} min, {wkt['total_kcal']:.0f} kcal){extra}"))
        elif wkt["junk_count"] > 0:
            L.append(_line("Workouts:", f"all {wkt['junk_count']} look implausible — judge from steps + exercise minutes"))

    # Body & gait
    L.append("")
    L.append("-- Body & gait " + "-" * 48)
    b = dom["body"]
    if b["status"] == "no_data":
        L.append("  no body-composition or gait data in window")
    else:
        if b["body_mass"] or b["bmi"] or b["body_fat"] or b["lean"]:
            parts = []
            if b["body_mass"]:
                parts.append(f"{b['body_mass']['value']:.1f} kg")
            if b["bmi"]:
                parts.append(f"BMI {b['bmi']['value']:.1f} [{b['b_bmi']}]")
            if b["body_fat"]:
                parts.append(f"body fat {b['body_fat']['pct']:.1f}%")
            if b["lean"]:
                parts.append(f"lean {b['lean']['value']:.1f} kg")
            # Date of the freshest reading shown, not a fixed metric order.
            present_body = [m for m in (b["body_mass"], b["bmi"], b["body_fat"], b["lean"]) if m and m.get("as_of")]
            as_of = max(present_body, key=lambda m: epoch(m["as_of"]) or 0.0, default=None)
            L.append(_line("Body (latest):", " · ".join(parts)))
            if as_of is not None:
                L.append(_line("", f"as of {fmt_local(as_of['as_of'])} — single reading, no trend"))
        if any([b["speed"], b["step_len"], b["asym"], b["dbl"]]):
            gparts = []
            if b["speed"]:
                gparts.append(f"speed {b['speed']['avg']:.2f} m/s [{b['b_speed']}]")
            if b["step_len"]:
                gparts.append(f"step {b['step_len']['avg']:.0f} cm")
            if b["asym"]:
                gparts.append(f"asym {b['asym']['avg']:.1f}% [{b['b_asym']}]")
            if b["dbl"]:
                gparts.append(f"dbl-support {b['dbl']['avg']:.1f}% [{b['b_dbl']}]")
            L.append(_line("Gait (avg):", " · ".join(gparts)))

    # Flags
    flags = report["flags"]
    if flags:
        L.append("")
        L.append("-- Flags " + "-" * 54)
        for f in flags:
            mark = "!" if f["severity"] == "caution" else "·"
            scope = f"[{f['scope']}] " if f["scope"] != "report" else ""
            L.append(f"  {mark} {scope}{f['message_en']}")

    # Footer
    L.append("")
    L.append("-" * 64)
    L.append("Consumer wearable data, not a medical diagnosis. Computed locally;")
    L.append("nothing is transmitted. Prefer 3/7-day averages; single nights/days")
    L.append("are noisy. If a pattern persists, consider talking to a clinician.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _expand_manifest_chunks(manifest_path: str, manifest: dict) -> tuple[Optional[dict], Optional[json.JSONDecodeError]]:
    """Read every day chunk referenced by a v4 manifest and merge into the flat
    v2-shaped snapshot that build_report() / _build_index() already understand.

    Returns (flat_data, None) on success. On a JSON-decode error inside any
    chunk, returns (None, exc) so the caller can surface it through the same
    parse_error gate the top-level file uses. Missing chunk files are skipped
    silently (iCloud may still be syncing; the digest will simply omit those
    days rather than abort)."""
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    merged_metrics: dict[str, list] = {}
    merged_sleep: list = []
    merged_workouts: list = []
    days = manifest.get("days") or []
    for entry in days:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        chunk_path = os.path.join(base_dir, rel)
        try:
            with open(chunk_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            continue  # iCloud not synced yet for this day; let the window shrink
        except OSError:
            continue
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, exc
        if not isinstance(chunk, dict):
            continue
        chunk_metrics = chunk.get("metrics")
        if isinstance(chunk_metrics, dict):
            for key, samples in chunk_metrics.items():
                if isinstance(samples, list):
                    merged_metrics.setdefault(key, []).extend(samples)
        chunk_sleep = chunk.get("sleep")
        if isinstance(chunk_sleep, list):
            merged_sleep.extend(chunk_sleep)
        chunk_workouts = chunk.get("workouts")
        if isinstance(chunk_workouts, list):
            merged_workouts.extend(chunk_workouts)

    # Synthesize the v2 shape downstream consumers expect. Use day boundaries
    # at 00:00:00 / 23:59:59 UTC so window_days math in build_report rounds
    # to the actual day count.
    first_date = days[0].get("date") if days and isinstance(days[0], dict) else None
    last_date = days[-1].get("date") if days and isinstance(days[-1], dict) else None
    return {
        "schemaVersion": EXPECTED_SCHEMA_VERSION,
        "generatedAt": manifest.get("generatedAt"),
        "rangeStart": f"{first_date}T00:00:00+00:00" if isinstance(first_date, str) else None,
        "rangeEnd": f"{last_date}T23:59:59+00:00" if isinstance(last_date, str) else None,
        "metrics": merged_metrics,
        "sleep": merged_sleep,
        "workouts": merged_workouts,
    }, None


def load_export_or_report(path: str, as_json: bool) -> tuple[Optional[dict], int]:
    """Open + parse the export.

    On success returns (data, 0). On the only two nonzero-exit conditions --
    missing file (2) or unparseable / non-object JSON (3) -- prints the same
    gate message the report mode uses and returns (None, code). Shared by the
    full-report mode and every query subcommand so the file gate is identical.

    Handles both shapes the HealthDrop export has used:
      * Legacy v2 single-file snapshot -- top-level metrics/sleep/workouts.
      * v4 chunked layout -- the file at `path` is a manifest pointing at
        days/YYYY-MM-DD.json chunks; we expand them in-place to the v2 shape
        so build_report and the SQLite index stay agnostic.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        msg = _flag("no_file", "caution")
        if as_json:
            print(json.dumps({"schema": "healthdrop.digest/1", "meta": {"status": "no_file"}, "flags": [msg]}, ensure_ascii=False))
        else:
            print("HealthDrop export not found at:")
            print(f"  {path}")
            print(msg["message_en"])
        # Diagnostics to stderr; data-absence is reported in the output too.
        print(f"error: file not found: {path}", file=sys.stderr)
        return None, 2
    except PermissionError:
        # macOS TCC blocks reads of `~/Library/Mobile Documents/iCloud~*` from
        # processes that lack Full Disk Access, which is the common case for
        # OpenClaw / Codex CLI / any non-Terminal launcher. Print actionable
        # guidance so the user knows what to fix rather than just an errno.
        msg = _flag("permission_denied", "caution")
        if as_json:
            print(json.dumps({"schema": "healthdrop.digest/1", "meta": {"status": "permission_denied"}, "flags": [msg]}, ensure_ascii=False))
        else:
            print("HealthDrop export exists but this process cannot read it:")
            print(f"  {path}")
            if "Mobile Documents" in path:
                # Quote in case the skill is installed under a path with
                # spaces (e.g. ~/Library/Application Support/...). Without
                # quoting, the copy-pasted hint splits on whitespace and
                # python complains "can't open file".
                examine_abs = _shell_quote(os.path.abspath(__file__))
                print()
                print("This is a macOS TCC restriction: iCloud app-private containers")
                print("are not readable from processes outside a Terminal with Full")
                print("Disk Access. Two fixes:")
                print()
                print("  A. Shell-hook mirror (recommended -- no extra TCC grant needed):")
                print(f"       python3 {examine_abs} setup-mirror --shell")
                print("     Adds a one-line hook to ~/.zshrc that refreshes the mirror at")
                print("     every new Terminal. The skill auto-prefers ~/.healthdrop/ on")
                print("     subsequent runs. (Or drop --shell for a 120s launchd refresh,")
                print("     which requires granting Full Disk Access to the python binary.)")
                print()
                print(f"  B. Point the skill at any readable path via env var:")
                print(f"       export {ENV_INPUT_OVERRIDE}=/path/to/healthdrop.json")
        print(f"error: permission denied: {path}", file=sys.stderr)
        return None, 2
    except OSError as exc:
        print(f"error: cannot read file: {exc}", file=sys.stderr)
        return None, 2

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = _flag("parse_error", "caution")
        if as_json:
            print(json.dumps({"schema": "healthdrop.digest/1", "meta": {"status": "parse_error"}, "flags": [msg]}, ensure_ascii=False))
        else:
            print("HealthDrop export is not valid JSON.")
            print(msg["message_en"])
        print(f"error: JSON parse failed: {exc}", file=sys.stderr)  # never echo the body
        return None, 3

    if not isinstance(data, dict):
        print("error: top-level JSON is not an object", file=sys.stderr)
        return None, 3

    # v4 manifest detection: top-level `days` list and no flat sample arrays.
    if (isinstance(data.get("days"), list)
            and not isinstance(data.get("metrics"), dict)
            and not isinstance(data.get("sleep"), list)
            and not isinstance(data.get("workouts"), list)):
        expanded, chunk_exc = _expand_manifest_chunks(path, data)
        if chunk_exc is not None:
            msg = _flag("parse_error", "caution")
            if as_json:
                print(json.dumps({"schema": "healthdrop.digest/1", "meta": {"status": "parse_error"}, "flags": [msg]}, ensure_ascii=False))
            else:
                print("HealthDrop day chunk is not valid JSON.")
                print(msg["message_en"])
            print(f"error: chunk JSON parse failed: {chunk_exc}", file=sys.stderr)
            return None, 3
        if expanded is not None:
            data = expanded

    return data, 0


# --------------------------------------------------------------------------- #
# Targeted query mode  (examine.py query ...)
#
# Low-cost slices the full digest does not expose: a per-day time series, one
# metric's stat over an arbitrary day range, and a single day across metrics.
#
# Read-side efficiency: instead of parsing the (potentially large, full-history)
# JSON on every query, queries read a compact SQLite index of per-metric-per-day
# aggregates, rebuilt only when the source file changes (size+mtime signature).
# When the source is unchanged the JSON is never opened, so query cost is bounded
# regardless of how much history the export carries. The index stores aggregates
# only (sum/cnt/min/max + the last reading, per metric per local day) -- never
# raw sample arrays -- so it stays small (~metrics x days) and privacy-safe.
# Same aggregation rules as the report: unit "%" is a 0-1 fraction (x100 at
# render), cumulative metrics use the daily SUM, everything else the daily MEAN.
# The full report (above) is unchanged; only the read/query side is indexed.
# --------------------------------------------------------------------------- #
_INDEX_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE daily(
    metric TEXT, day TEXT, sum REAL, cnt INTEGER, mn REAL, mx REAL,
    last_s REAL, last_val REAL
);
CREATE INDEX ix_daily ON daily(metric, day);
"""


def _parse_day_arg(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _range_label(lo: Optional[date], hi: Optional[date]) -> str:
    if lo and hi:
        return f"{lo.isoformat()}..{hi.isoformat()}"
    if lo:
        return f"from {lo.isoformat()}"
    if hi:
        return f"to {hi.isoformat()}"
    return "full window"


def _fmt_num(v: float) -> str:
    """Compact number: thousands-separated, drop a trailing .0."""
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}"


def _metric_kind(key: str) -> tuple[bool, str, float]:
    """(cumulative, display_unit, scale) for a catalog metric. A '%' unit is
    stored as a 0-1 fraction and shown as a percent (scale 100)."""
    unit = METRIC_CATALOG[key]
    pct = unit == "%"
    return key in CUMULATIVE_METRICS, ("%" if pct else unit), (100.0 if pct else 1.0)


def _day_value(dsum: float, cnt: int, cumulative: bool) -> float:
    """Per-day display value: cumulative -> daily sum; otherwise daily mean."""
    return dsum if cumulative else (dsum / cnt if cnt else 0.0)


def _index_path_for(src_path: str) -> str:
    """Local cache path for the derived index. Lives under ~/.cache (NOT the
    iCloud folder) so it never syncs; one index file per source path."""
    digest = hashlib.sha1(os.path.abspath(src_path).encode("utf-8")).hexdigest()[:16]
    cache_dir = os.path.expanduser("~/.cache/healthdrop")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"index-{digest}.sqlite")


def _aggregate_daily(data: dict) -> list[tuple]:
    """Per-(metric, local day) aggregates from the parsed snapshot. Stores RAW
    values (the % -> *100 conversion happens at render, as in the report) and
    tracks the chronologically last reading per day for `latest` queries."""
    metrics = as_dict(data.get("metrics"))
    rows: list[tuple] = []
    for key, samples in metrics.items():
        if not isinstance(samples, list):
            continue
        per_day: dict[date, list[float]] = {}  # day -> [sum, cnt, mn, mx, last_s, last_val]
        for s in samples:
            if not isinstance(s, dict):
                continue
            v = safe_num(s.get("value"))
            if v is None:
                continue
            dt = parse_dt(s.get("startDate"))
            if dt is None:
                continue
            d = dt.astimezone().date()  # local calendar day
            es = dt.timestamp()
            rec = per_day.get(d)
            if rec is None:
                per_day[d] = [v, 1.0, v, v, es, v]
            else:
                rec[0] += v
                rec[1] += 1.0
                rec[2] = min(rec[2], v)
                rec[3] = max(rec[3], v)
                if es >= rec[4]:
                    rec[4], rec[5] = es, v
        for d, rec in per_day.items():
            rows.append((key, d.isoformat(), rec[0], int(rec[1]), rec[2], rec[3], rec[4], rec[5]))
    return rows


def _build_index(db_path: str, data: dict, sig: str) -> None:
    """(Re)build the SQLite index atomically: write a temp DB, then os.replace."""
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(_INDEX_SCHEMA)
        con.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)", _aggregate_daily(data))
        meta = {
            "source_sig": sig,
            "generated_at": str(data.get("generatedAt") or ""),
            "range_start": str(data.get("rangeStart") or ""),
            "range_end": str(data.get("rangeEnd") or ""),
            "schema_version": str(data.get("schemaVersion") or ""),
            "sleep_count": str(len(as_list(data.get("sleep")))),
            "workout_count": str(len(as_list(data.get("workouts")))),
        }
        con.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
        con.commit()
    finally:
        con.close()
    os.replace(tmp, db_path)


def _input_signature(path: str) -> Optional[str]:
    """Compose a cache key from the manifest + every chunk it references.

    Including the days/ dir mtime catches add/remove/rename of chunks (e.g.
    a mirror tick that brings in new day files after a partial first run).
    Folding the (basename, size, mtime_ns) of every manifest-referenced
    chunk into a sha256 catches in-place rewrites of ANY chunk -- not just
    the latest day. The previous "latest_chunk only" signature missed
    historical HealthKit corrections, deletions, and backfills (those edit
    a non-latest chunk in place; the days/ dir mtime does not bump and the
    manifest file may not change either), so the SQLite index could stay
    stale against the rewritten chunk.

    Hashing keeps the signature fixed-length even for multi-year exports
    (~1500+ chunks); we only stat the chunks the manifest actually names,
    so cost scales with declared days, not with the dir contents. On any
    parse/stat failure we degrade gracefully to the legacy dir+latest
    signature so the cache check never crashes a query.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    parts = [f"manifest:{st.st_size}:{st.st_mtime_ns}"]
    base_dir = os.path.dirname(os.path.abspath(path))
    days_dir = os.path.join(base_dir, "days")
    try:
        days_st = os.stat(days_dir)
    except OSError:
        return "|".join(parts)
    parts.append(f"days_dir:{days_st.st_mtime_ns}")
    # Walk the manifest's declared chunks and fold each chunk's identity
    # into a sha256 digest. This is the only thing that catches an
    # in-place rewrite of a non-latest chunk.
    try:
        with open(path, "rb") as fh:
            manifest = json.loads(fh.read())
    except (OSError, json.JSONDecodeError):
        manifest = None
    referenced: list[str] = []
    if isinstance(manifest, dict):
        days_entries = manifest.get("days")
        if isinstance(days_entries, list):
            chunk_hash = hashlib.sha256()
            for entry in days_entries:
                if not isinstance(entry, dict):
                    continue
                rel = entry.get("path")
                if not isinstance(rel, str) or not rel:
                    continue
                basename = os.path.basename(rel)
                referenced.append(basename)
                chunk_path = os.path.join(base_dir, rel)
                try:
                    cst = os.stat(chunk_path)
                except OSError:
                    chunk_hash.update(f"{basename}:missing\n".encode("utf-8"))
                    continue
                chunk_hash.update(
                    f"{basename}:{cst.st_size}:{cst.st_mtime_ns}\n".encode("utf-8")
                )
            parts.append(f"chunks_sha256:{chunk_hash.hexdigest()}")
    if not referenced:
        # Manifest unreadable or non-v4 -- fall back to the legacy
        # latest-chunk hint so we still detect today's chunk mutating.
        try:
            entries = [n for n in os.listdir(days_dir) if n.endswith(".json")]
        except OSError:
            return "|".join(parts)
        if entries:
            latest = max(entries)
            try:
                latest_mtime_ns = os.stat(
                    os.path.join(days_dir, latest)
                ).st_mtime_ns
                parts.append(f"latest_chunk:{latest}:{latest_mtime_ns}")
            except OSError:
                pass
    return "|".join(parts)


def ensure_query_index(path: str, as_json: bool) -> tuple[Optional[str], int]:
    """Return (db_path, 0) with a fresh index, or (None, code) on the file gate.

    Fast path: when the index exists and the source's composite signature is
    unchanged (see _input_signature), the JSON is never opened. Otherwise the
    source is parsed once (reusing the report's file gate, so no_file=2 /
    parse_error=3 stay identical) and the index is rebuilt.
    """
    sig = _input_signature(path)
    db_path = _index_path_for(path)
    if sig is not None and os.path.exists(db_path):
        try:
            con = sqlite3.connect(db_path)
            row = con.execute("SELECT value FROM meta WHERE key = 'source_sig'").fetchone()
            con.close()
            if row and row[0] == sig:
                return db_path, 0  # source unchanged -> no parse
        except sqlite3.Error:
            pass  # corrupt/old index -> fall through and rebuild
    data, code = load_export_or_report(path, as_json)  # shared no_file/parse gate
    if data is None:
        return None, code
    if sig is None:
        sig = _input_signature(path) or "unknown"
    _build_index(db_path, data, sig)
    return db_path, 0


def _read_meta(con: sqlite3.Connection) -> dict:
    return {k: v for k, v in con.execute("SELECT key, value FROM meta").fetchall()}


def _meta_window_label(meta: dict) -> str:
    lo, hi = local_day(meta.get("range_start")), local_day(meta.get("range_end"))
    if lo and hi:
        return f"window {lo.isoformat()}..{hi.isoformat()}"
    return "window unknown"


def _resolve_range(
    meta: dict, days: Optional[int], dfrom: Optional[str], dto: Optional[str]
) -> tuple[Optional[date], Optional[date]]:
    """Inclusive day bounds. --from/--to win; else --days N anchored at the
    export's last day (range_end, else generatedAt); else the whole index."""
    if dfrom or dto:
        return (_parse_day_arg(dfrom) if dfrom else None, _parse_day_arg(dto) if dto else None)
    if days and days > 0:
        anchor = local_day(meta.get("range_end")) or local_day(meta.get("generated_at"))
        if anchor is not None:
            return anchor - timedelta(days=days - 1), anchor
    return None, None


def _requested_days(meta: dict, lo: Optional[date], hi: Optional[date], day_strs: list[str]) -> int:
    """Inclusive calendar-day count of the requested window, used as the divisor
    for cumulative per-day averages (no-data days count as 0). Explicit --from/--to
    or --days use those bounds; the full window uses the export's 24h span so it
    matches the report's headline divisor."""
    if lo is None and hi is None:
        rs, re = parse_dt(meta.get("range_start")), parse_dt(meta.get("range_end"))
        if rs and re and re > rs:
            return max(1, round((re - rs).total_seconds() / 86400.0))
        return max(1, len(day_strs))
    eff_lo = lo or (date.fromisoformat(day_strs[0]) if day_strs else None)
    eff_hi = hi or (date.fromisoformat(day_strs[-1]) if day_strs else None)
    if eff_lo and eff_hi and eff_hi >= eff_lo:
        return (eff_hi - eff_lo).days + 1
    return max(1, len(day_strs))


def _query_list(con: sqlite3.Connection, as_json: bool) -> int:
    meta = _read_meta(con)
    present_map = {
        m: (int(c), mn, mx)
        for m, c, mn, mx in con.execute(
            "SELECT metric, SUM(cnt), MIN(day), MAX(day) FROM daily GROUP BY metric"
        ).fetchall()
    }
    rows: list[dict] = []
    for key, unit in METRIC_CATALOG.items():
        info = present_map.get(key)
        rows.append(
            {
                "metric": key,
                "unit": unit,
                "samples": info[0] if info else 0,
                "first_day": info[1] if info else None,
                "last_day": info[2] if info else None,
                "present": info is not None,
                "cumulative": key in CUMULATIVE_METRICS,
            }
        )
    extra = sorted(m for m in present_map if m not in METRIC_CATALOG)
    sleep_n = int(meta.get("sleep_count", "0") or 0)
    workout_n = int(meta.get("workout_count", "0") or 0)

    if as_json:
        print(
            json.dumps(
                {
                    "schema": "healthdrop.query.list/1",
                    "window": _meta_window_label(meta),
                    "metrics": rows,
                    "extra_keys": extra,
                    "sleep_intervals": sleep_n,
                    "workouts": workout_n,
                },
                ensure_ascii=False,
            )
        )
        return 0

    present = [r for r in rows if r["present"]]
    empty = [r["metric"] for r in rows if not r["present"]]
    print(f"Metrics in export · {_meta_window_label(meta)}")
    print(f"  present ({len(present)}):")
    for r in present:
        span = f"{r['first_day']}..{r['last_day']}" if r["first_day"] else ""
        tag = " [cumulative]" if r["cumulative"] else ""
        print(f"    {r['metric']:<32} {r['samples']:>6} samples  {r['unit']:<11} {span}{tag}")
    if empty:
        print(f"  empty ({len(empty)}): " + ", ".join(empty))
    if extra:
        print("  uncatalogued keys present: " + ", ".join(extra))
    print(f"  sleep: {sleep_n} intervals · workouts: {workout_n}")
    return 0


def _query_metric(
    con: sqlite3.Connection,
    meta: dict,
    key: str,
    stat: str,
    days: Optional[int],
    dfrom: Optional[str],
    dto: Optional[str],
    as_json: bool,
) -> int:
    if key not in METRIC_CATALOG:
        if as_json:
            print(json.dumps({"error": "unknown_metric", "key": key, "hint": "examine.py query list"}, ensure_ascii=False))
        else:
            print(f"unknown metric '{key}'. Run: examine.py query list", file=sys.stderr)
        return 2

    for flag, val in (("--from", dfrom), ("--to", dto)):
        if val is not None and _parse_day_arg(val) is None:
            if as_json:
                print(json.dumps({"error": "invalid_date", "arg": flag, "value": val, "expected": "YYYY-MM-DD"}, ensure_ascii=False))
            else:
                print(f"error: invalid {flag} '{val}', expected YYYY-MM-DD", file=sys.stderr)
            return 2

    cumulative, disp_unit, scale = _metric_kind(key)

    lo, hi = _resolve_range(meta, days, dfrom, dto)
    sql = "SELECT day, sum, cnt, mn, mx, last_s, last_val FROM daily WHERE metric = ?"
    params: list[Any] = [key]
    if lo:
        sql += " AND day >= ?"
        params.append(lo.isoformat())
    if hi:
        sql += " AND day <= ?"
        params.append(hi.isoformat())
    sql += " ORDER BY day"
    drows = con.execute(sql, params).fetchall()

    # Per-day display value: cumulative -> daily sum, else daily mean (raw, *scale).
    series: list[tuple[str, float]] = []
    daily_sums: list[float] = []  # for cumulative avg/min/max
    mins: list[float] = []
    maxs: list[float] = []
    total_sum = 0.0
    total_cnt = 0
    for day, dsum, cnt, mn, mx, _, _ in drows:
        total_sum += dsum
        total_cnt += int(cnt)
        mins.append(mn)
        maxs.append(mx)
        daily_sums.append(dsum)
        series.append((day, _day_value(dsum, cnt, cumulative) * scale))

    latest_val: Optional[float] = None
    latest_day: Optional[str] = None
    if drows:
        last = drows[-1]  # max day (rows are ORDER BY day)
        latest_day = last[0]
        # columns: day(0) sum(1) cnt(2) mn(3) mx(4) last_s(5) last_val(6)
        latest_val = (last[1] if cumulative else last[6]) * scale  # daily sum vs last reading

    day_strs = [r[0] for r in drows]
    range_days = _requested_days(meta, lo, hi, day_strs)

    if cumulative:
        # Average over the requested calendar days (no-data days count as 0), not
        # only the days that have rows -- otherwise a gap inflates the per-day figure.
        a_avg = (total_sum / range_days) if range_days else None
        # A requested day with no data counts as 0 here too (consistent with the
        # average), so a gap surfaces as a 0 minimum; max is unaffected by zeros.
        a_min = (0.0 if len(drows) < range_days else min(daily_sums)) if daily_sums else None
        a_max = max(daily_sums) if daily_sums else None
    else:
        a_avg = (total_sum / total_cnt) if total_cnt else None
        a_min = min(mins) if mins else None
        a_max = max(maxs) if maxs else None

    def _sc(x: Optional[float]) -> Optional[float]:
        return x * scale if x is not None else None

    n = len(drows) if cumulative else total_cnt
    agg: dict[str, Optional[float]] = {
        "avg": _sc(a_avg),
        "min": _sc(a_min),
        "max": _sc(a_max),
        "sum": _sc(total_sum) if drows else None,
        "latest": latest_val,
    }

    if as_json:
        payload: dict[str, Any] = {
            "schema": "healthdrop.query.metric/1",
            "metric": key,
            "unit": disp_unit,
            "cumulative": cumulative,
            "range": {"from": lo.isoformat() if lo else None, "to": hi.isoformat() if hi else None},
            "n": n,
            "active_days": len(drows),
            "range_days": range_days,
            "aggregates": {k: agg[k] for k in ("avg", "min", "max", "sum", "latest")},
            "latest_day": latest_day,
            "series": [{"date": d, "value": v} for d, v in series],
        }
        if stat == "count":
            payload["value"] = n
        elif stat in ("avg", "min", "max", "sum", "latest"):
            payload["value"] = agg[stat]
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if not drows:
        print(f"{key} ({disp_unit}): no data in {_range_label(lo, hi)}")
        return 0

    if stat == "series":
        kind = "sum" if cumulative else "mean"
        print(f"{key} ({disp_unit}) · per-day {kind} · {_range_label(lo, hi)} · {len(series)} day(s)")
        for d, v in series:
            print(f"  {d}  {_fmt_num(v):>12}")
        return 0

    if stat == "count":
        print(f"{n}")  # a count of readings/days is unitless
        return 0

    if stat in ("avg", "min", "max", "sum", "latest"):
        val = agg[stat]
        print("n/a" if val is None else f"{_fmt_num(val)} {disp_unit}".rstrip())
        return 0

    # default: summary
    if cumulative:
        head = f"{key} ({disp_unit}) · {_range_label(lo, hi)} · per-day sums over {range_days} day(s)"
        if len(drows) < range_days:
            head += f", {len(drows)} with data"
    else:
        head = f"{key} ({disp_unit}) · {_range_label(lo, hi)} · {n} reading{'' if n == 1 else 's'}"
    bits: list[str] = []
    for k in ("avg", "min", "max"):
        val = agg[k]
        if val is not None:
            bits.append(f"{k} {_fmt_num(val)}")
    line = "  " + " · ".join(bits) if bits else "  (no numeric samples)"
    if latest_val is not None:
        line += f" · latest {_fmt_num(latest_val)}" + (f" ({latest_day})" if latest_day else "")
    print(head)
    print(line)
    return 0


def _query_day(con: sqlite3.Connection, day_str: str, as_json: bool) -> int:
    day = _parse_day_arg(day_str)
    if day is None:
        if as_json:
            print(json.dumps({"error": "invalid_date", "value": day_str, "expected": "YYYY-MM-DD"}, ensure_ascii=False))
        else:
            print(f"error: invalid date '{day_str}', expected YYYY-MM-DD", file=sys.stderr)
        return 2
    found = {
        m: (dsum, int(cnt))
        for m, dsum, cnt in con.execute(
            "SELECT metric, sum, cnt FROM daily WHERE day = ?", (day.isoformat(),)
        ).fetchall()
    }
    rows: list[dict] = []
    for key in METRIC_CATALOG:
        info = found.get(key)
        if not info:
            continue
        dsum, cnt = info
        cumulative, disp_unit, scale = _metric_kind(key)
        rows.append(
            {
                "metric": key,
                "unit": disp_unit,
                "value": _day_value(dsum, cnt, cumulative) * scale,
                "kind": "sum" if cumulative else "mean",
                "n": cnt,
            }
        )

    if as_json:
        print(json.dumps({"schema": "healthdrop.query.day/1", "date": day.isoformat(), "metrics": rows}, ensure_ascii=False))
        return 0
    if not rows:
        print(f"{day.isoformat()}: no metric data")
        return 0
    print(f"{day.isoformat()} (local):")
    for r in rows:
        print(f"  {r['metric']:<32} {_fmt_num(r['value']):>10} {r['unit']:<11} ({r['kind']} of {r['n']})")
    return 0


def cmd_query(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="examine.py query",
        description="Targeted, low-cost queries over the HealthDrop export (indexed; no full report).",
    )
    sub = p.add_subparsers(dest="qcmd", required=True)

    def _common(sp: argparse.ArgumentParser) -> None:
        # default=None (not DEFAULT_INPUT) so resolve_input() can tell the user
        # "I omitted the path, fill in the canonical iCloud default and apply
        # mirror auto-prefer" from "I explicitly typed the canonical iCloud
        # path, bypass the mirror." Help text still advertises the canonical
        # path so the UX is unchanged.
        sp.add_argument("input", nargs="?", default=None,
                        help=f"Path to healthdrop.json (default: {DEFAULT_INPUT}).")
        sp.add_argument("--input", dest="input_opt", default=None, help="Alternative way to pass the path.")
        sp.add_argument("--json", action="store_true", help="Machine-readable output.")

    _common(sub.add_parser("list", help="Which metrics are present/empty + the window."))

    pm = sub.add_parser("metric", help="One metric: a stat or the per-day series.")
    pm.add_argument("key", help="Metric key, e.g. restingHeartRate (see: query list).")
    _common(pm)
    pm.add_argument(
        "--stat",
        choices=["summary", "avg", "min", "max", "sum", "count", "latest", "series"],
        default="summary",
        help="What to report (default: summary = avg/min/max/latest).",
    )
    pm.add_argument("--days", type=int, default=None, help="Last N local days (anchored at the export's last day).")
    pm.add_argument("--from", dest="dfrom", default=None, help="Start day YYYY-MM-DD (inclusive).")
    pm.add_argument("--to", dest="dto", default=None, help="End day YYYY-MM-DD (inclusive).")

    pd = sub.add_parser("day", help="All metrics for one local calendar day.")
    pd.add_argument("date", help="YYYY-MM-DD.")
    _common(pd)

    args = p.parse_args(argv)
    user_path = args.input_opt or args.input
    resolved = resolve_input(user_path or DEFAULT_INPUT, defaulted=user_path is None)
    db_path, code = ensure_query_index(resolved, args.json)
    if db_path is None:
        return code
    con = sqlite3.connect(db_path)
    try:
        if args.qcmd == "list":
            return _query_list(con, args.json)
        if args.qcmd == "metric":
            return _query_metric(con, _read_meta(con), args.key, args.stat, args.days, args.dfrom, args.dto, args.json)
        if args.qcmd == "day":
            return _query_day(con, args.date, args.json)
    finally:
        con.close()
    return 2


# --------------------------------------------------------------------------- #
# Mirror mode  (examine.py setup-mirror / examine.py mirror)
#
# macOS guards `~/Library/Mobile Documents/iCloud~*` containers with TCC: only
# processes that the user has granted Full Disk Access can read them. OpenClaw,
# Codex CLI, and other agent launchers usually fail that check, so the iCloud
# export is unreadable even though it is sitting right there.
#
# The mirror pattern works around this: a small launchd user agent, owned by
# the user's interactive Terminal context (which CAN be granted FDA in a single
# system-settings click), copies the iCloud container into a plain home-relative
# directory (`~/.healthdrop/`). The mirror is identical in shape -- manifest +
# `days/YYYY-MM-DD.json` chunks -- so the rest of the skill keeps working
# unchanged. resolve_input() auto-prefers the mirror when it exists.
#
# Why launchd over a cron / login script: cron is restricted under SIP and
# launchd is the macOS-native way to run periodic user-space tasks. Why poll
# vs WatchPaths: iCloud's atomic-write dance does not always trigger WatchPaths
# cleanly, and the export rate (a few times per hour at most) does not justify
# the complexity. Poll every 120s by default.
# --------------------------------------------------------------------------- #
MIRROR_LABEL = "dev.keenranger.healthdrop.mirror"
MIRROR_DEFAULT_INTERVAL = 120  # seconds between mirror runs


def _icloud_documents_dir() -> str:
    """Source directory the mirror reads from -- iCloud container Documents/."""
    return os.path.dirname(os.path.expanduser(ICLOUD_INPUT))


def _ensure_private_mirror_dirs(mirror_root: str) -> None:
    """Create mirror_root + mirror_root/days as private (0o700) directories.

    The mirror holds raw HealthKit data that previously lived inside a
    TCC-protected iCloud container. A default umask of 022 would leave the
    mirror dir as 0o755 and any other local account / process could list
    contents. Tighten to 0o700 on create AND on existing dirs we own --
    we created them, so this isn't surprising user state.
    """
    days = os.path.join(mirror_root, "days")
    os.makedirs(mirror_root, mode=0o700, exist_ok=True)
    os.makedirs(days, mode=0o700, exist_ok=True)
    for d in (mirror_root, days):
        try:
            if os.stat(d).st_mode & 0o077:
                os.chmod(d, 0o700)
        except OSError:
            pass  # best-effort; permission tightening is hygiene, not load-bearing


def _verify_manifest_against_mirror(
    src_manifest: str, dest_days: str, source_days: str,
) -> Optional[str]:
    """Cheap pre-publication check: does the source manifest's days list
    match files we already have in the mirror's days/ dir?

    Returns None if publication is safe, or a short reason string for the
    first blocking mismatch. The job is to catch the TOCTOU window between
    phase 1 (chunk copy) and phase 2 (manifest publish) WITHOUT
    false-positive deferring on producer-side metadata drift.

    Three cases when manifest `sizeBytes` != mirror chunk size:
      (a) chunk missing from mirror -- always defer (reader would silently
          get partial data via _expand_manifest_chunks)
      (b) source chunk's actual size also disagrees with the manifest --
          the producer's per-entry sizeBytes is just stale (HealthDrop in
          practice records sizes that are not always re-stamped when the
          chunk is rewritten). Mirror correctly mirrors what's on iCloud,
          so publishing is safe -- readers will read the same bytes
          they'd read directly from the source.
      (c) source and mirror disagree -- our phase 1 missed a fresh
          version of the chunk. Defer; the next tick's mtime-driven copy
          picks it up.
    """
    try:
        with open(src_manifest, "rb") as fh:
            manifest = json.loads(fh.read())
    except (OSError, json.JSONDecodeError) as exc:
        # If we can't parse the source manifest, don't publish it -- the
        # downstream parse_error gate will surface the JSON problem
        # separately on the next reader run.
        return f"source manifest unreadable: {exc}"
    days = manifest.get("days") if isinstance(manifest, dict) else None
    if not isinstance(days, list):
        return None  # legacy v2-shaped or unstructured -- nothing to verify
    for entry in days:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        expected_size = entry.get("sizeBytes")
        if not isinstance(expected_size, int):
            continue  # producer omitted the hint; skip
        # `rel` is "days/YYYY-MM-DD.json" relative to the manifest's parent;
        # the mirror keeps the same layout, so look up by basename under
        # dest_days.
        basename = os.path.basename(rel)
        local = os.path.join(dest_days, basename)
        try:
            local_st = os.stat(local)
            actual_size = local_st.st_size
        except FileNotFoundError:
            # Case (a)
            return f"manifest references {basename} which is not mirrored"
        except OSError as exc:
            return f"mirror chunk {basename} unstatable: {exc}"
        # Always cross-check the source chunk's CURRENT mtime against the
        # mirror's mtime. _atomic_copy preserved source mtime when we copied,
        # so equal-size + equal-mtime means the mirror still matches what
        # iCloud has. Equal-size + DIFFERENT-mtime is the same-size-rewrite
        # race: source was atomically rewritten after our phase 1 copy with
        # different content but identical byte length, which the size check
        # alone cannot see. (HealthDrop's `sha256` field would have caught
        # this in principle, but it ships as stale 32-bit prefixes in
        # practice; mtime parity is reliable because we control the dest
        # mtime via os.utime in _atomic_copy.)
        src_path = os.path.join(source_days, basename)
        try:
            src_st = os.stat(src_path)
        except OSError:
            if actual_size != expected_size:
                return (
                    f"chunk {basename} size mismatch: manifest={expected_size}"
                    f" mirror={actual_size} (source unstatable)"
                )
            # Mirror agrees with manifest, source unreadable -- publish.
            continue
        if actual_size == expected_size:
            if src_st.st_mtime_ns != local_st.st_mtime_ns:
                # Same-size rewrite race: source was rewritten after phase 1
                # with the same length. The next tick's mtime-driven copy
                # will catch up; defer for now.
                return (
                    f"chunk {basename} same-size mtime drift: source rewrote"
                    f" after phase 1 (will re-copy next tick)"
                )
            continue
        if src_st.st_size == actual_size:
            # Case (b): mirror matches source; manifest metadata is stale.
            # Publish; reader will read the same bytes that are on iCloud.
            continue
        src_size = src_st.st_size
        # Case (c)
        return (
            f"chunk {basename} size: manifest={expected_size}"
            f" mirror={actual_size} source={src_size}"
        )
    return None


def _mirror_log(dest_root: str, message: str) -> None:
    """Append a timestamped line to the mirror's own log. Best-effort.

    Opens with mode 0o600 so a fresh log file is not world-readable even if
    the mirror dir's parent ended up group/other readable.
    """
    log_path = os.path.join(dest_root, "mirror-log.txt")
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} {message}\n")
    except OSError:
        pass  # logging is best-effort; never block the mirror


def _needs_copy(src: str, dst: str) -> bool:
    """True iff dst is missing or differs from src in size or mtime.

    Compares st_mtime_ns (nanosecond precision) rather than seconds-rounded
    st_mtime: iCloud and HealthDrop both atomically rewrite files, and a
    sub-second rewrite that preserves size would otherwise be silently
    skipped, leaving the mirror permanently stale for that file.
    """
    if not os.path.exists(dst):
        return True
    try:
        s_src = os.stat(src)
        s_dst = os.stat(dst)
    except OSError:
        return True
    return s_src.st_size != s_dst.st_size or s_src.st_mtime_ns != s_dst.st_mtime_ns


def _stage_source_file(src: str, staged_tmp: str) -> None:
    """Copy src bytes into staged_tmp, preserving mtime, WITHOUT promoting.

    Used by phase 2 of _do_mirror to capture the manifest bytes once so the
    same staged file can be both verified and (if it passes) promoted to
    the final destination. Closes the TOCTOU window where verifying src by
    path and then re-opening src by path lets iCloud swap the file between
    the two opens.

    Same shape as _atomic_copy minus the os.replace(): on OSError, the
    .tmp is unlinked and the exception re-raised. 0o600 like everything
    else in the mirror -- and we fchmod() the open fd explicitly because
    O_CREAT only sets perms when CREATING the file; an existing .tmp from
    a previous run would retain whatever mode it had and that mode would
    survive os.replace() onto the published path.
    """
    try:
        with open(src, "rb") as fh_src:
            tmp_fd = os.open(staged_tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.fchmod(tmp_fd, 0o600)
                fh_dst = os.fdopen(tmp_fd, "wb")
            except OSError:
                os.close(tmp_fd)
                raise
            with fh_dst:
                while True:
                    buf = fh_src.read(65536)
                    if not buf:
                        break
                    fh_dst.write(buf)
                s = os.fstat(fh_src.fileno())
        os.utime(staged_tmp, ns=(s.st_atime_ns, s.st_mtime_ns))
    except OSError:
        try:
            os.unlink(staged_tmp)
        except OSError:
            pass
        raise


def _atomic_copy(src: str, dst: str) -> None:
    """Copy src to dst via a `.tmp` sidecar + rename, preserving mtime.

    The mtime preservation is load-bearing: _needs_copy uses mtime to skip
    unchanged chunks, so a naive copy that resets mtime would force every
    chunk to re-copy on every mirror tick.

    Uses fstat() on the open source handle (NOT os.stat(src)) so the dest
    metadata describes the exact bytes that were copied. If iCloud
    atomically replaces src after we opened it -- same size, newer mtime --
    a path-based stat would stamp the dst with the new mtime even though
    we read the old contents; future _needs_copy() ticks would then
    perma-skip the dst and the mirror would silently rot.

    If any step raises, remove the half-written `.tmp` so the dest dir does
    not accumulate orphan sidecars across failed ticks (common when iCloud
    refuses to materialise an evicted file under the launchd-spawned
    process's TCC context -- read() then fails with EDEADLK partway).
    """
    tmp = dst + ".tmp"
    try:
        # Open the source BEFORE creating the .tmp fd: if iCloud denies the
        # source read with EDEADLK (common for evicted chunks under a
        # launchd context without Full Disk Access), the failure happens
        # inside open() and we never end up with a dangling tmp_fd. The
        # previous order leaked one fd per failed chunk, which on a fresh
        # backfill with ~1300 EDEADLKs would hit EMFILE and silently
        # break later copies and logging in the same run.
        with open(src, "rb") as fh_src:
            # O_CREAT mode 0o600 forces a private-by-default permission on
            # the mirrored file regardless of the process umask -- the
            # data was private in iCloud's TCC container and should stay
            # private in the mirror. fchmod() after open is load-bearing:
            # O_CREAT's mode argument is ignored when the file already
            # exists (a stale .tmp from a previous crashed run), so
            # without the explicit fchmod a permissive sidecar would
            # survive truncation and that mode would ride along through
            # os.replace() onto the published dst.
            tmp_fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.fchmod(tmp_fd, 0o600)
                fh_dst = os.fdopen(tmp_fd, "wb")
            except OSError:
                os.close(tmp_fd)
                raise
            with fh_dst:
                while True:
                    buf = fh_src.read(65536)
                    if not buf:
                        break
                    fh_dst.write(buf)
                s = os.fstat(fh_src.fileno())  # the file we actually read
        # ns= form so the stamped mtime matches _needs_copy's nanosecond
        # comparison; the (float, float) form rounds to filesystem-native
        # precision and would mismatch on later ticks.
        os.utime(tmp, ns=(s.st_atime_ns, s.st_mtime_ns))
        os.replace(tmp, dst)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _do_mirror(source_dir: str, dest_root: str, log_enabled: bool) -> int:
    """Sync manifest + days/ from source_dir into dest_root. Returns exit code.

    0 = ok (incl. nothing to do). 5 = at least one copy failed (most likely a
    macOS TCC denial -- the launchd-spawned python needs Full Disk Access).
    """
    source_dir = os.path.expanduser(source_dir)
    dest_root = os.path.expanduser(dest_root)
    dest_days = os.path.join(dest_root, "days")
    _ensure_private_mirror_dirs(dest_root)

    src_manifest = os.path.join(source_dir, "healthdrop.json")
    try:
        os.stat(src_manifest)
    except FileNotFoundError:
        # No manifest in iCloud yet (user has not exported, or sync still
        # pending). Exit 0 with a short log line so the agent keeps polling
        # without tripping launchd's ThrottleInterval or spamming repeats.
        if log_enabled:
            _mirror_log(dest_root, f"tick skipped: source manifest not found at {src_manifest}")
        return 0
    except OSError as exc:
        # Stat failed for a non-FileNotFound reason: most often TCC denial
        # on the launchd-spawned mirror, sometimes EIO from iCloud
        # coordination. Treat as a real failure -- the user's verification
        # step is "tail mirror-log.txt and expect errors=0", and a silent
        # exit 0 here would make a TCC setup mistake look like success.
        if log_enabled:
            _mirror_log(dest_root, f"manifest stat failed: {exc}")
        return 5
    dst_manifest = os.path.join(dest_root, "healthdrop.json")

    copied_manifest = False
    chunks_copied = 0
    chunks_skipped = 0
    errors = 0

    # PHASE 1 -- copy day chunks BEFORE replacing the manifest. The manifest
    # is the index readers consult to find days/*.json; publishing a fresh
    # manifest while one of its referenced chunks failed to copy would let
    # a reader auto-prefer the mirror, look up a day from the new manifest,
    # find no/old chunk, and silently produce a partial answer
    # (_expand_manifest_chunks skips missing chunks on purpose). Mirror the
    # chunks first so the manifest's promises are never ahead of reality.
    src_days = os.path.join(source_dir, "days")
    try:
        entries = sorted(os.listdir(src_days))
    except FileNotFoundError:
        entries = []  # no days/ yet -- legitimately fine, e.g. first-export user
    except OSError as exc:
        errors += 1
        if log_enabled:
            _mirror_log(dest_root, f"days/ listdir failed: {exc}")
        entries = []
    for name in entries:
        if not name.endswith(".json"):
            continue
        src = os.path.join(src_days, name)
        dst = os.path.join(dest_days, name)
        if _needs_copy(src, dst):
            try:
                _atomic_copy(src, dst)
                chunks_copied += 1
            except OSError as exc:
                errors += 1
                if log_enabled:
                    # EDEADLK (errno 11) from an iCloud-container read means
                    # the kernel's brc/iCloud coordination refused to fault
                    # the file in for this process -- almost always because
                    # the launchd-spawned python lacks Full Disk Access.
                    hint = ""
                    if getattr(exc, "errno", None) == 11:
                        hint = "  [iCloud refused to materialise -- grant Full Disk Access to the python binary]"
                    _mirror_log(dest_root, f"chunk {name} copy failed: {exc}{hint}")
        else:
            chunks_skipped += 1

    # PHASE 2 -- publish the manifest only if every chunk we touched
    # succeeded AND the manifest's per-day promises match what we have on
    # disk. If anything failed (TCC, EIO, ...) or a TOCTOU race made the
    # source manifest reference chunks we didn't copy / copied a stale
    # version of, keep the previous manifest in place so readers continue
    # to see a self-consistent snapshot until the next tick resolves it.
    # A reader following the OLD manifest may miss the latest day's data,
    # but the rest of the snapshot stays coherent -- strictly better than
    # serving a NEW manifest that promises chunks we don't have.
    if _needs_copy(src_manifest, dst_manifest):
        if errors > 0:
            if log_enabled:
                _mirror_log(
                    dest_root,
                    f"manifest publication deferred: {errors} chunk error(s) this tick",
                )
        else:
            # TOCTOU fix: stage src_manifest into a verified-temp file FIRST,
            # verify the staged bytes, then atomically promote the SAME bytes
            # to dst_manifest. Previously we verified src by path and then
            # _atomic_copy reopened src by path -- iCloud could swap the
            # file between those two opens, letting us publish a manifest
            # whose chunk promises we never actually verified.
            staged_tmp = dst_manifest + ".tmp"
            staged_ok = False
            try:
                _stage_source_file(src_manifest, staged_tmp)
                staged_ok = True
            except OSError as exc:
                errors += 1
                if log_enabled:
                    _mirror_log(dest_root, f"manifest stage failed: {exc}")
            if staged_ok:
                mismatch_reason = _verify_manifest_against_mirror(
                    staged_tmp, dest_days, src_days,
                )
                if mismatch_reason is not None:
                    # Staged manifest references chunks we didn't (yet)
                    # mirror correctly -- could be a fresh export landed
                    # mid-tick whose new chunks haven't synced from iCloud
                    # yet, or a chunk we copied was rewritten on the source
                    # between phase 1 and now.
                    errors += 1
                    if log_enabled:
                        _mirror_log(
                            dest_root,
                            f"manifest publication deferred: {mismatch_reason}",
                        )
                    try:
                        os.unlink(staged_tmp)
                    except OSError:
                        pass
                else:
                    try:
                        os.replace(staged_tmp, dst_manifest)
                        copied_manifest = True
                    except OSError as exc:
                        errors += 1
                        if log_enabled:
                            _mirror_log(dest_root, f"manifest promote failed: {exc}")
                        try:
                            os.unlink(staged_tmp)
                        except OSError:
                            pass

    if log_enabled:
        _mirror_log(
            dest_root,
            f"tick manifest={int(copied_manifest)} copied={chunks_copied} "
            f"skipped={chunks_skipped} errors={errors}",
        )
    return 0 if errors == 0 else 5


def cmd_mirror(argv: list[str]) -> int:
    """Run one mirror tick. Designed to be invoked by launchd, not interactively."""
    p = argparse.ArgumentParser(
        prog="examine.py mirror",
        description="Copy the iCloud HealthDrop container into a TCC-free mirror dir. "
                    "Intended to be called by the launchd agent or shell hook installed "
                    "via setup-mirror.",
    )
    p.add_argument("--source", default=None,
                   help="Source iCloud Documents directory (default: canonical container).")
    p.add_argument("--dest", default="~/.healthdrop",
                   help="Destination mirror directory (default: ~/.healthdrop).")
    p.add_argument("--log", action="store_true",
                   help="Append a one-line tick summary to {dest}/mirror-log.txt.")
    p.add_argument("--lock", action="store_true",
                   help="Skip this tick (exit 0) if another mirror is already running. "
                        "Uses an fcntl exclusive lock on {dest}/.mirror.lock so that "
                        "concurrent shell opens (the --shell hook flow) don't race.")
    args = p.parse_args(argv)

    source = args.source or _icloud_documents_dir()
    # Normalize to abspath: when launchd / a shell hook calls us with a
    # relative --dest, the runtime cwd may differ from where the user
    # configured the mirror, and a relative path would silently land
    # elsewhere.
    dest_root = os.path.abspath(os.path.expanduser(args.dest))
    args.dest = dest_root

    if args.lock:
        import fcntl
        _ensure_private_mirror_dirs(dest_root)
        lock_path = os.path.join(dest_root, ".mirror.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another mirror is in flight; drop out quietly.
            os.close(lock_fd)
            return 0
        try:
            return _do_mirror(source, args.dest, args.log)
        finally:
            os.close(lock_fd)  # implicit flock release on close
    return _do_mirror(source, args.dest, args.log)


def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{MIRROR_LABEL}.plist")


def _install_mirror_agent(mirror_root: str, source_dir: str, interval: int) -> int:
    # abspath() after expanduser(): launchd spawns the mirror with cwd=/ , so
    # any relative path baked into the plist would resolve against / and
    # either fail or silently mirror to the wrong place. Same for the
    # HEALTHDROP_EXPORT_PATH hint that resolve_input() reads.
    mirror_root = os.path.abspath(os.path.expanduser(mirror_root))
    source_dir = os.path.abspath(os.path.expanduser(source_dir))
    _ensure_private_mirror_dirs(mirror_root)

    plist_path = _plist_path()
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)

    examine_path = os.path.abspath(__file__)
    python_path = sys.executable

    plist: dict[str, Any] = {
        "Label": MIRROR_LABEL,
        "ProgramArguments": [
            python_path,
            examine_path,
            "mirror",
            "--source", source_dir,
            "--dest", mirror_root,
            # --lock: when launchd is the sole writer this is a no-op (no
            # other holder), but if the user also installs the --shell hook
            # both writers target the same dest and would otherwise race on
            # `.tmp` rename. fcntl makes them serialise harmlessly.
            "--lock",
            "--log",
        ],
        "RunAtLoad": True,
        "StartInterval": interval,
        "StandardOutPath": os.path.join(mirror_root, "mirror-launchd.log"),
        "StandardErrorPath": os.path.join(mirror_root, "mirror-launchd.err"),
        "ProcessType": "Background",
        "Nice": 10,
    }
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)

    uid = os.getuid()
    print("setup-mirror: installed.")
    print(f"  label      : {MIRROR_LABEL}")
    print(f"  plist      : {plist_path}")
    print(f"  python     : {python_path}")
    print(f"  source dir : {source_dir}")
    print(f"  mirror dir : {mirror_root}")
    print(f"  interval   : every {interval}s")
    default_mirror = os.path.abspath(os.path.expanduser("~/.healthdrop"))
    if mirror_root != default_mirror:
        # resolve_input() only auto-prefers ~/.healthdrop/healthdrop.json. A
        # custom mirror root is invisible to it, so default-path queries
        # would still try the iCloud container and hit the same TCC wall.
        # Tell the user how to wire the mirror back into the lookup.
        print()
        print("NOTE: non-default mirror dir. Auto-prefer logic only sees")
        print(f"      ~/.healthdrop -- so set HEALTHDROP_EXPORT_PATH to make")
        print(f"      readers find this mirror:")
        # Quote the path: mirror_root may contain spaces or shell metacharacters,
        # which would otherwise split the export line apart when pasted.
        print(f"        export HEALTHDROP_EXPORT_PATH={_shell_quote(os.path.join(mirror_root, 'healthdrop.json'))}")

    # Cross-variant disclosure: if the user already has a shell hook installed
    # somewhere, both variants will write to the same dest. --lock makes them
    # safe, but redundant; surface so the user can clean up if they meant to
    # switch modes.
    hook_rcs = [rc for rc in _candidate_shell_rcs()
                if _rc_has_hook_block(rc)]
    if hook_rcs:
        print()
        print("NOTE: a shell hook is also installed at:")
        for rc in hook_rcs:
            print(f"        {rc}")
        print("      Both variants can coexist (both use --lock) but they're")
        print("      redundant. To drop the shell hook(s):")
        print(f"        python3 {examine_path} setup-mirror --uninstall")
    print()
    print("Required next steps (run in this order):")
    print()
    print("1. Grant Full Disk Access to the python binary above")
    print("   (System Settings -> Privacy & Security -> Full Disk Access -> +)")
    print(f"     {python_path}")
    print("   Without this the launchd-spawned mirror cannot fault iCloud-")
    print("   evicted day chunks (logs `Resource deadlock avoided`).")
    print()
    print("2. Load the agent and trigger the first tick:")
    print()
    # bootout first so a reinstall picks up the new ProgramArguments
    # (--interval / --source / --mirror-root). bootstrap is a no-op if
    # the label is already loaded, which means launchd would otherwise
    # keep the previous in-memory plist and silently ignore the edit.
    # On a first install bootout exits non-zero with "No such process";
    # that is expected, hence the `|| true`.
    print(f"     launchctl bootout gui/{uid}/{MIRROR_LABEL} || true   # ignore 'No such process' on a first install")
    print(f"     launchctl bootstrap gui/{uid} {plist_path}")
    print(f"     launchctl kickstart -k gui/{uid}/{MIRROR_LABEL}")
    print()
    # Quote any path that might contain spaces / shell metacharacters
    # (mirror_root, examine_path). Without quoting, a user installed under
    # ~/Library/Application Support/... gets a verification command whose
    # tail/ls/python3 args fragment on whitespace and report failure even
    # though the agent and mirror are healthy.
    mirror_log = _shell_quote(os.path.join(mirror_root, "mirror-log.txt"))
    mirror_days = _shell_quote(os.path.join(mirror_root, "days"))
    examine_q = _shell_quote(examine_path)
    print("3. Verify:")
    print(f"     tail -1 {mirror_log}   # expect errors=0")
    print(f"     ls {mirror_days} | wc -l           # matches the source count")
    print(f"     python3 {examine_q} query list   # mirror is auto-preferred")
    print()
    print(f"To remove later:  python3 {examine_q} setup-mirror --uninstall")
    return 0


def _uninstall_mirror_agent() -> int:
    plist_path = _plist_path()
    uid = os.getuid()
    bootout_failed_for_real = False

    try:
        boot = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{MIRROR_LABEL}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        # launchctl is macOS-only. On other platforms (or pruned PATH) skip
        # the bootout and just remove the plist if present.
        print("launchctl not found; skipping bootout (macOS-only)")
    else:
        if boot.returncode == 0:
            print(f"launchctl bootout: ok ({MIRROR_LABEL})")
        else:
            # Distinguish the benign "agent wasn't loaded" case from a real
            # launchd failure (permission denied, bad domain, etc.). If
            # bootout failed for a real reason, the agent is still running
            # in memory and removing the plist below would NOT stop it --
            # collapsing that into a successful uninstall would falsely tell
            # the user the mirror has stopped firing when it hasn't.
            msg = (boot.stderr or boot.stdout or "").strip() or "not loaded"
            benign_markers = ("no such process", "could not find service", "not loaded")
            is_benign = any(m in msg.lower() for m in benign_markers)
            if is_benign:
                print(f"launchctl bootout: {msg}")
            else:
                print(
                    f"launchctl bootout FAILED: {msg}",
                    file=sys.stderr,
                )
                print(
                    "  the agent may still be loaded in memory and will keep firing"
                    " ticks until reboot. fix the launchd error and re-run --uninstall.",
                    file=sys.stderr,
                )
                bootout_failed_for_real = True

    if os.path.exists(plist_path):
        try:
            os.remove(plist_path)
            print(f"removed: {plist_path}")
        except OSError as exc:
            print(f"could not remove {plist_path}: {exc}")
            return 1
    else:
        print(f"plist not present: {plist_path}")

    print()
    print("The mirror directory (~/.healthdrop/) is intentionally kept so cached")
    print("data is not lost. Delete it manually if you no longer want the mirror.")
    # Surface a non-zero exit code when bootout failed for a real reason
    # (not "not loaded"). Caller / script downstream of this command needs
    # to know the agent might still be live in memory.
    return 1 if bootout_failed_for_real else 0


SHELL_HOOK_BEGIN = "# >>> healthdrop mirror (auto-installed by setup-mirror --shell) >>>"
SHELL_HOOK_END = "# <<< healthdrop mirror <<<"


def _default_shell_rc() -> str:
    """Pick the default rc file to manage. zsh is macOS's default since
    Catalina; bash users can override with --shell-rc.

    Fish is intentionally NOT supported via --shell -- the snippet uses
    POSIX subshell-and-background syntax that fish does not parse.
    """
    return os.path.expanduser("~/.zshrc")


def _candidate_shell_rcs() -> list[str]:
    """All POSIX rc files a previous --shell install may have written to.

    Used by `--uninstall` (without an explicit --shell-rc) to scan and
    clean every plausible location, so a user who installed via
    `--shell-rc ~/.bashrc` and later runs `--uninstall` without the same
    flag still ends up with a clean rc set.
    """
    return [os.path.expanduser(p) for p in (
        "~/.zshrc",
        "~/.bashrc",
        "~/.bash_profile",
        "~/.profile",
    )]


def _is_fish_rc(rc_path: str) -> bool:
    p = rc_path.lower()
    return p.endswith(".fish") or "/fish/" in p


def _rc_has_hook_block(rc_path: str) -> bool:
    """True iff the sentinel BEGIN marker appears in rc_path. Cheap check
    used by the cross-variant warning."""
    try:
        with open(rc_path, "r", encoding="utf-8") as fh:
            return SHELL_HOOK_BEGIN in fh.read()
    except OSError:
        return False


def _shell_quote(s: str) -> str:
    """POSIX-safe single-quote wrap, escaping any embedded single quotes."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _build_shell_snippet(python_path: str, examine_path: str,
                         source_dir: str, mirror_root: str) -> str:
    """Render the sentinel-delimited block that goes in the rc file.

    source_dir and mirror_root are baked in even when they match defaults
    so that a future skill update changing the defaults does not silently
    redirect an already-installed user's mirror.
    """
    py = _shell_quote(python_path)
    script = _shell_quote(examine_path)
    src = _shell_quote(source_dir)
    dst = _shell_quote(mirror_root)
    return (
        f"{SHELL_HOOK_BEGIN}\n"
        "# Fire one mirror tick per new interactive shell. Inherits the parent\n"
        "# Terminal's Full Disk Access via TCC, so no extra grant is needed.\n"
        "# Background-forked (& inside a subshell) so shell startup is not blocked.\n"
        f"# Manage: python3 {examine_path} setup-mirror --shell [--uninstall]\n"
        f"( {py} {script} mirror --source {src} --dest {dst} --lock --log >/dev/null 2>&1 & ) 2>/dev/null\n"
        f"{SHELL_HOOK_END}\n"
    )


class _MalformedHookBlock(Exception):
    """Raised when a sentinel BEGIN appears without a matching END (or vice
    versa) in the rc file. Refuse to "strip" in that case -- naïvely walking
    to EOF would delete every user line written below the BEGIN marker."""


def _strip_hook_block(content: str) -> str:
    """Remove the sentinel-delimited block from content, if present.

    Leaves the rest of the file byte-identical when no block matches, so a
    no-op uninstall doesn't rewrite the rc and disturb its mtime / signing
    state. Idempotent for well-formed blocks.

    Raises _MalformedHookBlock if the rc has a BEGIN without END or an END
    without BEGIN -- a previous manual edit, partial copy, or interrupted
    write can leave the rc in that state, and silently stripping to EOF
    would delete every user line written below the BEGIN marker. Callers
    handle this as a non-fatal "refusing to touch" so the user can fix
    by hand.
    """
    # Walk the file line-by-line and verify each BEGIN is closed by an END
    # before the next BEGIN appears, and that no END appears outside a block.
    # A simple "are both substrings present?" check (the previous impl) lets
    # BEGIN-BEGIN-END or END-before-BEGIN slip through, after which the strip
    # loop happily drops every user line under an orphan BEGIN.
    skip = False
    begin_count = 0
    end_count = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == SHELL_HOOK_BEGIN:
            if skip:
                # nested / unclosed BEGIN: previous block was never terminated
                raise _MalformedHookBlock(
                    "BEGIN sentinel without matching END"
                )
            skip = True
            begin_count += 1
            continue
        if stripped == SHELL_HOOK_END:
            if not skip:
                # END before any BEGIN, or a second END after the block closed
                raise _MalformedHookBlock(
                    "END sentinel without matching BEGIN"
                )
            skip = False
            end_count += 1
            continue
    if skip:
        # walked off EOF while still inside a block
        raise _MalformedHookBlock("BEGIN sentinel without matching END")
    if begin_count == 0:
        # no block present and no orphan markers -- return unchanged so the
        # rc's mtime / signing state is preserved on a no-op uninstall.
        return content

    # Markers balance. Do the actual strip pass. Same shape as the validator
    # above so behavior stays in lock-step if the sentinels ever change.
    out: list[str] = []
    skip = False
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == SHELL_HOOK_BEGIN:
            skip = True
            continue
        if stripped == SHELL_HOOK_END:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "".join(out)


def _install_shell_hook(rc_path: str, mirror_root: str, source_dir: str) -> int:
    if _is_fish_rc(rc_path):
        # The snippet uses POSIX `( cmd & ) 2>/dev/null` subshell-and-background
        # syntax which fish does not parse the same way. Rather than ship a
        # half-working fish hook, refuse and suggest an explicit path.
        print(f"--shell does not support fish rc files yet: {rc_path}")
        print()
        print("The default snippet is POSIX-only (zsh/bash). For fish, run the")
        print(f"mirror manually from a fish prompt instead:")
        print(f"  {sys.executable} {os.path.abspath(__file__)} mirror --lock --log")
        print("or set up a fish-native background invocation in your config.fish.")
        return 1

    # abspath() after expanduser() so the snippet records a stable absolute
    # path; the rc file is sourced from arbitrary cwds across new shells and
    # a relative path would silently rebind the mirror per session.
    mirror_root = os.path.abspath(os.path.expanduser(mirror_root))
    source_dir = os.path.abspath(os.path.expanduser(source_dir))
    _ensure_private_mirror_dirs(mirror_root)

    examine_path = os.path.abspath(__file__)
    python_path = sys.executable

    existing = ""
    try:
        with open(rc_path, "r", encoding="utf-8") as fh:
            existing = fh.read()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        print(f"could not read {rc_path}: {exc}")
        return 1

    try:
        cleaned = _strip_hook_block(existing)
    except _MalformedHookBlock as exc:
        # A user-edited rc with mismatched sentinels means we cannot safely
        # replace the block without potentially deleting their other content.
        # Refuse loudly rather than guess.
        print(f"refusing to install: rc has malformed hook markers ({exc})")
        print(f"  fix by hand:  {rc_path}")
        print( "  then re-run setup-mirror --shell.")
        return 1
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    new_content = cleaned + _build_shell_snippet(
        python_path, examine_path, source_dir, mirror_root,
    )

    # Preserve the existing rc file's mode (0o600 etc.) so users who store
    # secrets in their rc do not see their file demoted to 0o644 by our
    # temp-file + replace. If the rc did not exist, create it 0o600 -- rc
    # files commonly hold tokens and 0o644-default is the wrong choice.
    try:
        existing_mode = os.stat(rc_path).st_mode & 0o777
    except OSError:
        existing_mode = 0o600
    # Resolve symlinks before writing: many users keep ~/.zshrc as a symlink
    # into a dotfiles checkout. os.replace(tmp, link) replaces the SYMLINK
    # with a regular file, silently breaking the dotfiles mapping. Write to
    # the realpath instead so the managed target is what gets updated.
    target_path = os.path.realpath(rc_path)
    try:
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        tmp = target_path + ".tmp.healthdrop"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.chmod(tmp, existing_mode)
        os.replace(tmp, target_path)
    except OSError as exc:
        print(f"could not write {rc_path}: {exc}")
        return 1

    print("setup-mirror --shell: installed.")
    print(f"  rc file    : {rc_path}")
    print(f"  python     : {python_path}")
    print(f"  examine    : {examine_path}")
    print(f"  source dir : {source_dir}")
    print(f"  mirror dir : {mirror_root}")

    default_mirror = os.path.abspath(os.path.expanduser("~/.healthdrop"))
    if mirror_root != default_mirror:
        # Same disclosure the launchd installer prints: resolve_input() only
        # auto-prefers the hard-coded ~/.healthdrop default, so a custom
        # --mirror-root needs an env override to be visible to readers.
        print()
        print("NOTE: non-default mirror dir. Auto-prefer logic only sees")
        print(f"      ~/.healthdrop -- so set HEALTHDROP_EXPORT_PATH to make")
        print(f"      readers find this mirror:")
        # Quote the path: mirror_root may contain spaces or shell metacharacters,
        # which would otherwise split the export line apart when pasted.
        print(f"        export HEALTHDROP_EXPORT_PATH={_shell_quote(os.path.join(mirror_root, 'healthdrop.json'))}")

    if os.path.exists(_plist_path()):
        print()
        print("NOTE: a launchd mirror agent is also installed. Both variants")
        print("      can coexist (both use --lock) but they're redundant. To")
        print("      drop the launchd one:")
        print(f"        launchctl bootout gui/{os.getuid()}/{MIRROR_LABEL}")
        print(f"        rm {_plist_path()}")

    print()
    print("How it works:")
    print("  - Every new interactive shell fires `mirror --lock --log` in the")
    print("    background. The --lock prevents concurrent shells from racing.")
    print("  - No extra TCC grant: the mirror inherits the Terminal's existing")
    print("    Full Disk Access.")
    print("  - Trade-off vs launchd mode: the mirror only refreshes when you")
    print("    open a new shell, not every 120s. Fine for most use cases.")
    print()
    # First-tick command bakes in the configured paths -- without --source
    # and --dest a custom-mirror-root install would populate the default
    # ~/.healthdrop on the verification run (a different mirror than the
    # snippet writes), making the immediate tail show errors / nothing.
    # Quote every printed path so a mirror_root / source_dir / examine_path
    # with spaces survives copy-paste.
    py_q = _shell_quote(python_path)
    ex_q = _shell_quote(examine_path)
    src_q = _shell_quote(source_dir)
    dst_q = _shell_quote(mirror_root)
    mirror_log = _shell_quote(os.path.join(mirror_root, "mirror-log.txt"))
    rc_q = _shell_quote(rc_path)
    print("Trigger the first tick now:")
    print(f"  {py_q} {ex_q} mirror --source {src_q} --dest {dst_q} --lock --log")
    print(f"  tail -1 {mirror_log}   # expect errors=0")
    print()
    # The custom --shell-rc must round-trip through --uninstall, otherwise the
    # default scan only covers ~/.zshrc + the bash family and leaves the hook
    # alive in places like ~/.config/zsh/.zshrc.
    if rc_path == _default_shell_rc():
        print(f"To remove: python3 {ex_q} setup-mirror --uninstall")
    else:
        print(f"To remove: python3 {ex_q} setup-mirror --uninstall --shell-rc {rc_q}")
    return 0


def _uninstall_shell_hook(rc_path: str) -> bool:
    """Return True if a hook block was found and removed, False if no block
    is present in the rc.

    Raises OSError if the rc has a block but cannot be read or rewritten
    (e.g. read-only filesystem, permission denied). Raises
    _MalformedHookBlock if the rc has only one of the BEGIN/END sentinels;
    refusing to touch is safer than guessing where the block ends.

    Both exception cases let cmd_setup_mirror --uninstall distinguish "no
    work to do" from "the user's hook is still installed and we couldn't
    remove it", which the old bool-only return collapsed into the same
    "no block found" message.
    """
    try:
        with open(rc_path, "r", encoding="utf-8") as fh:
            existing = fh.read()
    except FileNotFoundError:
        return False

    cleaned = _strip_hook_block(existing)  # may raise _MalformedHookBlock
    if cleaned == existing:
        return False  # no block found

    try:
        existing_mode = os.stat(rc_path).st_mode & 0o777
    except OSError:
        existing_mode = 0o600
    # Same dotfiles-symlink concern as _install_shell_hook: write to the
    # realpath so we update the managed target rather than replacing the
    # symlink with a regular file.
    target_path = os.path.realpath(rc_path)
    tmp = target_path + ".tmp.healthdrop"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(cleaned)
        os.chmod(tmp, existing_mode)  # preserve rc perms; never demote 0o600 to 0o644
        os.replace(tmp, target_path)
    except OSError:
        # Re-raise so cmd_setup_mirror --uninstall can distinguish a real
        # write failure from "no block found" and return non-zero. Returning
        # False here would let the command exit 0 with "no shell hook block
        # found" while the hook is actually still installed.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def cmd_setup_mirror(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="examine.py setup-mirror",
        description="Install (or remove) a macOS background sync that mirrors the iCloud "
                    "HealthDrop container into a TCC-free directory. Two install modes: "
                    "launchd user agent (default; refreshes every N seconds but requires "
                    "Full Disk Access on the python binary) or shell hook (--shell; "
                    "refreshes on each new Terminal session, no extra TCC grant needed).",
    )
    p.add_argument("--uninstall", action="store_true",
                   help="Remove whichever variant(s) are installed (launchd + shell hook). "
                        "Mirror dir is kept so cached data is not lost.")
    p.add_argument("--shell", action="store_true",
                   help="Install the shell-hook variant instead of launchd. Appends a "
                        "sentinel-delimited block to your shell rc (~/.zshrc by default) "
                        "that fires `examine.py mirror --lock` in the background on each "
                        "new interactive shell.")
    p.add_argument("--shell-rc", default=None,
                   help="Path to the shell rc file to manage (default ~/.zshrc). "
                        "Only meaningful with --shell or --uninstall.")
    p.add_argument("--interval", type=int, default=MIRROR_DEFAULT_INTERVAL,
                   help=f"Launchd-mode only: seconds between mirror ticks "
                        f"(default {MIRROR_DEFAULT_INTERVAL}).")
    p.add_argument("--mirror-root", default="~/.healthdrop",
                   help="Where to mirror the iCloud container (default ~/.healthdrop).")
    p.add_argument("--source", default=None,
                   help="Override the source iCloud Documents directory.")
    args = p.parse_args(argv)

    # abspath() the custom --shell-rc the same way mirror_root/source are
    # normalised. A relative `--shell-rc rc` installed from one cwd would
    # otherwise have a printed uninstall hint that, when run from a
    # different cwd, edits a different file (or reports no hook found)
    # and leaves the original hook firing forever.
    rc_path = (
        os.path.abspath(os.path.expanduser(args.shell_rc))
        if args.shell_rc else _default_shell_rc()
    )

    if args.uninstall:
        # Idempotent cleanup: try both variants regardless of which one (or
        # both) the user originally installed. Each helper is a no-op if the
        # corresponding artefact is absent.
        plist_code = _uninstall_mirror_agent()
        # If the user gave an explicit --shell-rc, only touch that file.
        # Otherwise scan every POSIX rc this installer could have written
        # to, so an earlier `--shell --shell-rc ~/.bashrc` install still
        # cleans up when the user later runs `--uninstall` without the
        # flag. Without this scan, a hook in .bashrc would keep spawning
        # mirror ticks forever.
        rcs = [rc_path] if args.shell_rc else _candidate_shell_rcs()
        removed_any = False
        errors_any = False
        for rc in rcs:
            try:
                if _uninstall_shell_hook(rc):
                    print(f"removed shell hook block from {rc}")
                    removed_any = True
            except _MalformedHookBlock as exc:
                # Hook markers present but malformed -- refuse to touch and
                # surface non-zero so the user knows the hook is still live.
                print(f"malformed hook markers in {rc} ({exc}); not modifying", file=sys.stderr)
                errors_any = True
            except OSError as exc:
                # rc has the hook but we couldn't rewrite it. Surface
                # non-zero so a script following the printed "ok" does not
                # assume removal succeeded.
                print(f"could not rewrite {rc}: {exc}", file=sys.stderr)
                errors_any = True
        if not removed_any and not errors_any:
            target = rc_path if args.shell_rc else "any known rc"
            print(f"no shell hook block found in {target}")
        # plist_code is launchd-side; or together so any failure propagates.
        return 1 if (errors_any or plist_code != 0) else 0

    source = args.source or _icloud_documents_dir()
    if args.shell:
        return _install_shell_hook(rc_path, args.mirror_root, source)
    return _install_mirror_agent(args.mirror_root, source, args.interval)


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "query":
        return cmd_query(raw_argv[1:])
    if raw_argv and raw_argv[0] == "setup-mirror":
        return cmd_setup_mirror(raw_argv[1:])
    if raw_argv and raw_argv[0] == "mirror":
        return cmd_mirror(raw_argv[1:])

    parser = argparse.ArgumentParser(
        description="Examine the user's HealthDrop iCloud export and print a privacy-safe health digest.",
        epilog="Subcommand: 'query' for targeted slices, e.g. examine.py query metric stepCount --days 7",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,  # sentinel; see resolve_input() docstring for why
        help="Path to healthdrop.json (default: canonical iCloud path).",
    )
    parser.add_argument("--input", dest="input_opt", default=None, help="Alternative way to pass the input path.")
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable DigestReport JSON.")
    parser.add_argument("--lang", choices=["ko", "en"], default=None, help="Render-language hint (JSON carries both).")
    args = parser.parse_args(raw_argv)

    user_path = args.input_opt or args.input
    path = resolve_input(user_path or DEFAULT_INPUT, defaulted=user_path is None)
    data, code = load_export_or_report(path, args.json)
    if data is None:
        return code

    report = build_report(data)
    if args.lang:
        report.setdefault("render", {})["language_hint"] = args.lang

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
