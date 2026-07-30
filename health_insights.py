"""Turn daily metrics into baselines, deviations, flags and associations.

Everything here is computed against *your own* rolling baseline rather than
population norms: the useful question for wearable data is almost never "is this
value normal for a human" but "is this value normal for me, this month".

Not medical advice. Flags are prompts to pay attention, not diagnoses.
"""

from __future__ import annotations

import csv
import math
import os
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any
from collections.abc import Sequence

import health_history
from health_history import HistoryResult
from health_metrics import CARRY_FORWARD

# Rolling baseline configuration
BASELINE_WINDOW = 60      # days of history each baseline looks back over
BASELINE_MIN_N = 14       # fewest observations before a baseline is trusted
REGULARITY_WINDOW = 14    # days used for sleep-timing regularity

# Metrics that get a rolling baseline + deviation + z-score.
BASELINE_METRICS = [
    'resting_hr', 'hrv_sdnn', 'respiratory_rate', 'spo2_avg', 'wrist_temp_c',
    'sleep_asleep_hours', 'sleep_deep_hours', 'sleep_rem_hours',
    'sleep_efficiency_pct', 'breathing_disturbances', 'walking_hr',
]

# Direction that counts as "worse" for each baselined metric.
WORSE_WHEN_HIGH = {'resting_hr', 'respiratory_rate', 'wrist_temp_c', 'breathing_disturbances', 'walking_hr'}

SLEEP_TARGET_HOURS = 7.5

# Training-load ratio (acute 7d mean / chronic 28d mean). The 0.8-1.3 band is
# the widely used "sweet spot"; above ~1.5 is the classic spike associated with
# elevated injury risk in the sports-science literature.
ACWR_LOW = 0.8
ACWR_HIGH = 1.5

ILLNESS_MIN_SIGNALS = 2


# ---------------------------------------------------------------------------
# Small statistics helpers
# ---------------------------------------------------------------------------

def mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def stdev(values: Sequence[float]) -> float | None:
    return statistics.pstdev(values) if len(values) > 1 else None


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def correlation_t(r: float, n: int) -> float | None:
    """t statistic for a correlation; |t| > 2 is roughly p < 0.05."""
    if n < 3 or abs(r) >= 1.0:
        return None
    return r * math.sqrt((n - 2) / (1 - r * r))


# Circular SD grows without bound as times approach a uniform spread around the
# clock, so it is capped. Anything near this is already "as irregular as it gets".
MAX_CIRCULAR_SD_HOURS = 6.0


def circular_stats(hours: Sequence[float]) -> tuple[float, float] | None:
    """Mean and SD of clock times, in hours.

    Sleep midpoints straddle midnight (23.8 and 0.2 are 24 minutes apart, not
    23.6 hours), so a plain mean/SD would be meaningless. Treats each time as an
    angle on a 24-hour circle.

    Times spread evenly around the clock produce a near-zero resultant vector.
    That is *maximum* dispersion, not absent data, so it reports the cap rather
    than returning None — otherwise the most irregular sleeper possible would
    show a blank instead of a warning.
    """
    if len(hours) < 2:
        return None
    angles = [2 * math.pi * h / 24.0 for h in hours]
    c = statistics.mean(math.cos(a) for a in angles)
    s = statistics.mean(math.sin(a) for a in angles)
    r = math.hypot(c, s)

    mean_hour = (math.atan2(s, c) % (2 * math.pi)) * 24.0 / (2 * math.pi)
    if r <= 1e-9:
        return mean_hour, MAX_CIRCULAR_SD_HOURS

    sd_hours = math.sqrt(max(-2.0 * math.log(min(r, 1.0)), 0.0)) * 24.0 / (2 * math.pi)
    return mean_hour, min(sd_hours, MAX_CIRCULAR_SD_HOURS)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Series handling
# ---------------------------------------------------------------------------

def to_float(value: Any) -> float | None:
    if value in ('', None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_series(rows: list[dict[str, Any]], column: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for row in rows:
        val = to_float(row.get(column))
        if val is not None:
            out[date.fromisoformat(row['date'])] = val
    return out


def carry_forward(series: dict[date, float], dates: list[date], max_gap_days: int = 45) -> dict[date, float]:
    """Hold the last measurement forward — a weight taken Monday is still the
    best estimate for Tuesday — but not indefinitely past a long gap."""
    out: dict[date, float] = {}
    last_val: float | None = None
    last_day: date | None = None
    for d in dates:
        if d in series:
            last_val, last_day = series[d], d
        if last_val is not None and last_day is not None and (d - last_day).days <= max_gap_days:
            out[d] = last_val
    return out


def rolling_baseline(
    series: dict[date, float],
    dates: list[date],
    window: int = BASELINE_WINDOW,
    min_n: int = BASELINE_MIN_N,
) -> dict[date, tuple[float, float | None]]:
    """Trailing mean/SD over `window` days, excluding the day itself.

    Excluding the current day matters: a baseline that contains today's value
    partly cancels the deviation it is supposed to measure.
    """
    out: dict[date, tuple[float, float | None]] = {}
    for d in dates:
        history = [
            series[d - timedelta(days=k)]
            for k in range(1, window + 1)
            if (d - timedelta(days=k)) in series
        ]
        if len(history) >= min_n:
            out[d] = (statistics.mean(history), stdev(history))
    return out


def rolling_mean(series: dict[date, float], day: date, days: int) -> float | None:
    vals = [series[day - timedelta(days=k)] for k in range(days) if (day - timedelta(days=k)) in series]
    return statistics.mean(vals) if vals else None


# ---------------------------------------------------------------------------
# Scores and flags
# ---------------------------------------------------------------------------

def z_to_score(z: float, worse_when_high: bool) -> float:
    """Map a z-score to 0-100, where 70 is "at baseline"."""
    directed = -z if worse_when_high else z
    return clamp(70.0 + directed * 15.0)


def recovery_score(parts: dict[str, float | None]) -> tuple[float | None, list[str]]:
    """Transparent weighted blend of recovery signals, 0-100.

    Weights are a judgement call, not a validated instrument — the component
    breakdown is emitted alongside so the number is auditable rather than magic.
    """
    weights = {
        'hrv': 0.30,
        'resting_hr': 0.25,
        'sleep_duration': 0.25,
        'sleep_efficiency': 0.10,
        'respiratory_rate': 0.10,
    }
    total_w = 0.0
    total = 0.0
    used: list[str] = []
    for key, weight in weights.items():
        val = parts.get(key)
        if val is None:
            continue
        total += val * weight
        total_w += weight
        used.append(key)
    if total_w < 0.5:
        return None, used
    return round(total / total_w, 1), used


def illness_signals(row: dict[str, Any]) -> list[str]:
    """Multi-signal physiological strain check.

    Any one of these moves on its own for boring reasons (a hard workout, a warm
    room, a late meal). Consumer wearables treat *simultaneous* movement across
    several as the meaningful pattern, so this only reports the combination.
    """
    signals: list[str] = []

    temp_dev = to_float(row.get('wrist_temp_c_dev'))
    if temp_dev is not None and temp_dev >= 0.4:
        signals.append(f'wrist temp +{temp_dev:.2f}degC')

    rhr_dev = to_float(row.get('resting_hr_dev'))
    rhr_z = to_float(row.get('resting_hr_z'))
    if rhr_dev is not None and rhr_z is not None and rhr_dev >= 3.0 and rhr_z >= 1.0:
        signals.append(f'resting HR +{rhr_dev:.1f} bpm')

    hrv_z = to_float(row.get('hrv_sdnn_z'))
    if hrv_z is not None and hrv_z <= -1.0:
        signals.append(f'HRV {hrv_z:.1f} SD below baseline')

    rr_dev = to_float(row.get('respiratory_rate_dev'))
    rr_z = to_float(row.get('respiratory_rate_z'))
    if rr_dev is not None and rr_z is not None and rr_dev >= 1.0 and rr_z >= 1.0:
        signals.append(f'respiratory rate +{rr_dev:.1f}/min')

    spo2_z = to_float(row.get('spo2_avg_z'))
    if spo2_z is not None and spo2_z <= -1.5:
        signals.append(f'SpO2 {spo2_z:.1f} SD below baseline')

    return signals


# ---------------------------------------------------------------------------
# Daily insights table
# ---------------------------------------------------------------------------

def build_daily_insights(
    daily_rows: list[dict[str, Any]],
    start: date | None = None,
) -> list[dict[str, Any]]:
    rows = [r for r in daily_rows if not start or date.fromisoformat(r['date']) >= start]
    if not rows:
        return []

    dates = [date.fromisoformat(r['date']) for r in rows]
    series = {col: build_series(rows, col) for col in BASELINE_METRICS}
    baselines = {col: rolling_baseline(series[col], dates) for col in BASELINE_METRICS}

    load_series = build_series(rows, 'exercise_minutes')
    midpoint_series = build_series(rows, 'sleep_midpoint_hour')

    out: list[dict[str, Any]] = []
    for row, d in zip(rows, dates, strict=True):
        rec: dict[str, Any] = {'date': row['date'], 'wear_class': row.get('wear_class', 'none')}

        for col in BASELINE_METRICS:
            value = series[col].get(d)
            base = baselines[col].get(d)
            if value is None or base is None:
                rec[f'{col}_baseline'] = ''
                rec[f'{col}_dev'] = ''
                rec[f'{col}_z'] = ''
                continue
            mu, sd = base
            rec[f'{col}_baseline'] = round(mu, 2)
            rec[f'{col}_dev'] = round(value - mu, 2)
            rec[f'{col}_z'] = round((value - mu) / sd, 2) if sd and sd > 0 else ''

        # --- recovery components
        parts: dict[str, float | None] = {}
        hrv_z = to_float(rec.get('hrv_sdnn_z'))
        if hrv_z is not None:
            parts['hrv'] = z_to_score(hrv_z, worse_when_high=False)
        rhr_z = to_float(rec.get('resting_hr_z'))
        if rhr_z is not None:
            parts['resting_hr'] = z_to_score(rhr_z, worse_when_high=True)
        rr_z = to_float(rec.get('respiratory_rate_z'))
        if rr_z is not None:
            parts['respiratory_rate'] = z_to_score(rr_z, worse_when_high=True)

        asleep = to_float(row.get('sleep_asleep_hours'))
        if asleep is not None:
            parts['sleep_duration'] = clamp(100.0 * asleep / SLEEP_TARGET_HOURS)
        efficiency = to_float(row.get('sleep_efficiency_pct'))
        if efficiency is not None:
            parts['sleep_efficiency'] = clamp(efficiency)

        score, used = recovery_score(parts)
        rec['recovery_score'] = score if score is not None else ''
        rec['recovery_inputs'] = '+'.join(used)

        # --- illness / strain signals
        signals = illness_signals(rec)
        rec['strain_signal_count'] = len(signals)
        rec['strain_flag'] = 'yes' if len(signals) >= ILLNESS_MIN_SIGNALS else ''
        rec['strain_detail'] = '; '.join(signals)

        # --- training load ratio
        acute = rolling_mean(load_series, d, 7)
        chronic = rolling_mean(load_series, d, 28)
        rec['load_acute_7d'] = round(acute, 1) if acute is not None else ''
        rec['load_chronic_28d'] = round(chronic, 1) if chronic is not None else ''
        if acute is not None and chronic and chronic > 0:
            ratio = acute / chronic
            rec['load_ratio'] = round(ratio, 2)
            rec['load_status'] = 'spike' if ratio > ACWR_HIGH else ('detraining' if ratio < ACWR_LOW else 'steady')
        else:
            rec['load_ratio'] = ''
            rec['load_status'] = ''

        # --- sleep timing regularity
        window = [
            midpoint_series[d - timedelta(days=k)]
            for k in range(REGULARITY_WINDOW)
            if (d - timedelta(days=k)) in midpoint_series
        ]
        stats = circular_stats(window) if len(window) >= 5 else None
        if stats:
            _mu, sd_hours = stats
            rec['sleep_regularity_sd_min'] = round(sd_hours * 60, 1)
        else:
            rec['sleep_regularity_sd_min'] = ''

        out.append(rec)

    return out


DAILY_INSIGHT_COLUMNS = (
    ['date', 'wear_class']
    + [f'{c}{suffix}' for c in BASELINE_METRICS for suffix in ('_baseline', '_dev', '_z')]
    + ['recovery_score', 'recovery_inputs', 'strain_signal_count', 'strain_flag', 'strain_detail',
       'load_acute_7d', 'load_chronic_28d', 'load_ratio', 'load_status', 'sleep_regularity_sd_min']
)


# ---------------------------------------------------------------------------
# Associations
# ---------------------------------------------------------------------------

ASSOCIATIONS = [
    ('sleep_asleep_hours', 'hrv_sdnn', 1, 'Sleep duration vs next-day HRV'),
    ('sleep_asleep_hours', 'resting_hr', 1, 'Sleep duration vs next-day resting HR'),
    ('sleep_deep_hours', 'hrv_sdnn', 1, 'Deep sleep vs next-day HRV'),
    ('sleep_efficiency_pct', 'hrv_sdnn', 1, 'Sleep efficiency vs next-day HRV'),
    ('steps', 'sleep_deep_hours', 1, 'Daily steps vs next-night deep sleep'),
    ('exercise_minutes', 'sleep_efficiency_pct', 1, 'Exercise minutes vs next-night sleep efficiency'),
    ('daylight_minutes', 'sleep_asleep_hours', 1, 'Daylight exposure vs next-night sleep duration'),
    ('active_kcal', 'hrv_sdnn', 1, 'Active energy vs next-day HRV'),
    ('effort_vigorous_min', 'resting_hr', 1, 'Vigorous effort vs next-day resting HR'),
    ('breathing_disturbances', 'sleep_efficiency_pct', 0, 'Breathing disturbances vs same-night sleep efficiency'),
    ('wrist_temp_c', 'hrv_sdnn', 0, 'Wrist temperature vs same-day HRV'),
    ('sleep_midpoint_hour', 'sleep_asleep_hours', 0, 'Sleep timing vs sleep duration'),
    ('diet_protein_g', 'body_mass_kg', 7, 'Protein intake vs weight one week later'),
]

MIN_PAIRS = 30


def analyse_associations(
    daily_rows: list[dict[str, Any]],
    start: date | None,
    min_pairs: int = MIN_PAIRS,
) -> list[dict[str, Any]]:
    rows = [r for r in daily_rows if not start or date.fromisoformat(r['date']) >= start]
    results: list[dict[str, Any]] = []

    for driver, outcome, lag, label in ASSOCIATIONS:
        ds = build_series(rows, driver)
        os_ = build_series(rows, outcome)
        if not ds or not os_:
            continue
        pairs = [
            (v, os_[d + timedelta(days=lag)])
            for d, v in ds.items()
            if (d + timedelta(days=lag)) in os_
        ]
        if len(pairs) < min_pairs:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        r = pearson(xs, ys)
        if r is None:
            continue
        t = correlation_t(r, len(pairs))
        results.append({
            'label': label,
            'driver': driver,
            'outcome': outcome,
            'lag_days': lag,
            'n': len(pairs),
            'r': round(r, 3),
            't': round(t, 2) if t is not None else '',
            'notable': 'yes' if t is not None and abs(t) > 2.0 else '',
        })

    results.sort(key=lambda x: -abs(x['r']))
    return results


def threshold_contrast(
    daily_rows: list[dict[str, Any]],
    driver: str,
    outcome: str,
    threshold: float,
    lag: int,
    start: date | None,
) -> dict[str, Any] | None:
    """Compare the outcome on days following a low vs high driver value.

    Often more legible than a correlation coefficient: "nights under 6h cost you
    4 ms of HRV" lands where "r = -0.21" does not.
    """
    rows = [r for r in daily_rows if not start or date.fromisoformat(r['date']) >= start]
    ds = build_series(rows, driver)
    os_ = build_series(rows, outcome)
    low: list[float] = []
    high: list[float] = []
    for d, v in ds.items():
        target = os_.get(d + timedelta(days=lag))
        if target is None:
            continue
        (low if v < threshold else high).append(target)
    if len(low) < 8 or len(high) < 8:
        return None
    lo, hi = statistics.mean(low), statistics.mean(high)
    return {
        'driver': driver,
        'outcome': outcome,
        'threshold': threshold,
        'lag_days': lag,
        'low_n': len(low),
        'low_mean': round(lo, 2),
        'high_n': len(high),
        'high_mean': round(hi, 2),
        'difference': round(hi - lo, 2),
    }


CONTRASTS = [
    ('sleep_asleep_hours', 'hrv_sdnn', 6.0, 1),
    ('sleep_asleep_hours', 'resting_hr', 6.0, 1),
    ('sleep_deep_hours', 'resting_hr', 1.0, 1),
    ('steps', 'sleep_deep_hours', 8000.0, 1),
    ('daylight_minutes', 'sleep_asleep_hours', 60.0, 1),
    ('exercise_minutes', 'hrv_sdnn', 30.0, 1),
]


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

TREND_METRICS = [
    ('resting_hr', 'bpm', False),
    ('hrv_sdnn', 'ms', True),
    ('vo2max', 'ml/kg/min', True),
    ('sleep_asleep_hours', 'h', True),
    ('sleep_deep_hours', 'h', True),
    ('sleep_efficiency_pct', '%', True),
    ('body_mass_kg', 'kg', None),
    ('body_fat_pct', '%', False),
    ('walking_speed_kmh', 'km/h', True),
    ('walking_hr', 'bpm', False),
    ('steps', 'steps', True),
    ('exercise_minutes', 'min', True),
    ('spo2_avg', '%', True),
]


# Occasionally-measured metrics need this many *real* readings in each period
# before a change between them counts as a trend.
MIN_MEASUREMENTS_FOR_TREND = 3


def compute_trends(
    daily_rows: list[dict[str, Any]],
    start: date | None,
    recent_days: int = 30,
    prior_days: int = 90,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Recent vs prior period comparison, plus the trends deliberately withheld.

    Carry-forward is what makes this dangerous: holding a weigh-in forward turns
    two real measurements into thirty rows, and a naive mean over those rows
    presents a device swap or a single bad bioimpedance reading as a confident
    multi-week trend. So gating is on the count of genuine measurements, never
    on the number of filled rows.
    """
    rows = [r for r in daily_rows if not start or date.fromisoformat(r['date']) >= start]
    if not rows:
        return [], []
    all_dates = sorted(date.fromisoformat(r['date']) for r in rows)
    last_day = all_dates[-1]
    recent_from = last_day - timedelta(days=recent_days)
    prior_from = recent_from - timedelta(days=prior_days)

    out: list[dict[str, Any]] = []
    withheld: list[dict[str, str]] = []

    for col, unit, higher_better in TREND_METRICS:
        measured = build_series(rows, col)
        recent_measured = sum(1 for d in measured if d > recent_from)
        prior_measured = sum(1 for d in measured if prior_from < d <= recent_from)

        if col in CARRY_FORWARD and (recent_measured < MIN_MEASUREMENTS_FOR_TREND
                                     or prior_measured < MIN_MEASUREMENTS_FOR_TREND):
            withheld.append({
                'metric': col,
                'reason': f'only {recent_measured} measurement(s) in the last {recent_days} days '
                          f'and {prior_measured} in the {prior_days} before — too few to call a trend',
            })
            continue

        series = carry_forward(measured, all_dates) if col in CARRY_FORWARD else measured
        recent = [v for d, v in series.items() if d > recent_from]
        prior = [v for d, v in series.items() if prior_from < d <= recent_from]
        if len(recent) < 5 or len(prior) < 5:
            continue

        r_mean, p_mean = statistics.mean(recent), statistics.mean(prior)
        delta = r_mean - p_mean
        pooled_sd = stdev(list(series.values()))
        direction = 'flat'
        if pooled_sd and abs(delta) > 0.25 * pooled_sd:
            direction = 'up' if delta > 0 else 'down'
        verdict = ''
        if direction != 'flat' and higher_better is not None:
            verdict = 'improving' if (direction == 'up') == higher_better else 'worsening'

        out.append({
            'metric': col,
            'unit': unit,
            'recent_mean': round(r_mean, 2),
            'recent_n': len(recent),
            'recent_measured': recent_measured,
            'prior_mean': round(p_mean, 2),
            'prior_n': len(prior),
            'prior_measured': prior_measured,
            'change': round(delta, 2),
            'pct_change': round(100.0 * delta / p_mean, 1) if p_mean else '',
            'direction': direction,
            'verdict': verdict,
        })
    return out, withheld


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

THIN_COVERAGE_PCT = 60.0


def thinly_covered_metrics(coverage: list[dict[str, Any]]) -> list[str]:
    """Key history metrics measured on too few days to lean on.

    Sparse-by-design metrics are included rather than excused: VO2 max really is
    emitted a handful of times a month, and a model reading a VO2 max percentile
    in llm_context.md has no other way to learn that.
    """
    out: list[str] = []
    for row in coverage:
        column = row.get('column')
        if column not in health_history.METRICS_BY_COLUMN:
            continue
        pct = to_float(row.get('coverage_pct_since_reliable_start'))
        if pct is not None and pct < THIN_COVERAGE_PCT:
            out.append(f'{column} ({pct:.0f}% of days)')
    return out


def _fmt(value: Any, suffix: str = '') -> str:
    if value in ('', None):
        return 'n/a'
    return f'{value}{suffix}'


def render_insights_report(
    insight_rows: list[dict[str, Any]],
    trends: list[dict[str, Any]],
    associations: list[dict[str, Any]],
    contrasts: list[dict[str, Any]],
    start: date | None,
    coverage: list[dict[str, Any]],
    withheld_trends: list[dict[str, str]] | None = None,
    history: HistoryResult | None = None,
) -> str:
    lines: list[str] = ['# Health insights', '']
    if start:
        lines.append(f'Analysis window opens **{start.isoformat()}**, the first date on which the '
                     'continuously-tracked core metrics (resting HR, HRV, staged sleep) are all live. '
                     'Everything before that is device history, not signal.')
        lines.append('')

    wear = defaultdict(int)
    for r in insight_rows:
        wear[r.get('wear_class', 'none')] += 1
    total = sum(wear.values())
    if total:
        lines.append(f'**{total} days analysed** — '
                     + ', '.join(f'{wear.get(k, 0)} {k}' for k in ('full', 'partial', 'minimal', 'none'))
                     + ' wear.')
        lines.append('')

    # --- historical anchor
    #
    # Deliberately above recovery and trends: a recovery score and a 30-vs-90-day
    # trend are both computed against a baseline that drifts with you, so on their
    # own they can describe a two-year decline as "steady". The capacity table is
    # the only place the report says how far today is from what you have held.
    if history is not None:
        lines.extend(health_history.render_history_highlights(history))

    # --- current state
    scored = [r for r in insight_rows if r.get('recovery_score') != '']
    if scored:
        recent = scored[-14:]
        avg = statistics.mean(float(r['recovery_score']) for r in recent)
        latest = scored[-1]
        lines.extend([
            '## Recovery',
            '',
            f"- Latest recovery score: **{latest['recovery_score']}/100** ({latest['date']})",
            f'- 14-day average: **{avg:.1f}/100**',
            '- Score blends HRV (30%), resting HR (25%), sleep duration (25%), '
            'sleep efficiency (10%) and respiratory rate (10%), each scored against your own '
            '60-day rolling baseline where 70 means "at baseline".',
            '',
        ])

    # --- strain / illness
    flagged = [r for r in insight_rows if r.get('strain_flag') == 'yes']
    lines.extend(['## Physiological strain flags', ''])
    if flagged:
        lines.append(f'{len(flagged)} day(s) where at least {ILLNESS_MIN_SIGNALS} recovery signals '
                     'moved the wrong way together — the pattern consumer wearables use for '
                     'illness onset. Most recent:')
        lines.append('')
        for r in flagged[-10:]:
            lines.append(f"- **{r['date']}** ({r['strain_signal_count']} signals) — {r['strain_detail']}")
    else:
        lines.append('No days where two or more strain signals moved together. That is the good outcome.')
    lines.append('')

    # --- sleep regularity
    reg = [float(r['sleep_regularity_sd_min']) for r in insight_rows if r.get('sleep_regularity_sd_min') != '']
    if reg:
        latest_reg = reg[-1]
        band = ('excellent' if latest_reg < 30 else 'good' if latest_reg < 60
                else 'irregular' if latest_reg < 90 else 'very irregular')
        lines.extend([
            '## Sleep timing regularity',
            '',
            f'- Current 14-day sleep-midpoint variability: **{latest_reg:.0f} min** ({band})',
            f'- Median across the window: **{statistics.median(reg):.0f} min**',
            '- Computed with circular statistics so times either side of midnight compare correctly. '
            'Consistency of sleep *timing* tracks metabolic and cardiovascular outcomes '
            'somewhat independently of sleep *duration*.',
            '',
        ])

    # --- training load
    load = [r for r in insight_rows if r.get('load_ratio') != '']
    if load:
        latest = load[-1]
        spikes = sum(1 for r in load if r.get('load_status') == 'spike')
        lines.extend([
            '## Training load',
            '',
            f"- Latest acute:chronic ratio: **{latest['load_ratio']}** ({latest['load_status']}) — "
            f"7-day mean {latest['load_acute_7d']} min/day vs 28-day mean {latest['load_chronic_28d']} min/day",
            f'- Days in spike territory (ratio > {ACWR_HIGH}): **{spikes}**',
            f'- Ratio between {ACWR_LOW} and {ACWR_HIGH} is the commonly cited sweet spot; '
            'sustained spikes are the pattern associated with elevated injury risk.',
            '',
        ])

    # --- trends
    if trends:
        lines.extend(['## Trends — last 30 days vs the 90 before', '',
                      '| Metric | Recent | Prior | Change | Direction | Readings |',
                      '|---|---|---|---|---|---|'])
        for t in trends:
            verdict = f" ({t['verdict']})" if t['verdict'] else ''
            readings = f"{t['recent_measured']} vs {t['prior_measured']}"
            lines.append(
                f"| {t['metric']} ({t['unit']}) | {t['recent_mean']} | {t['prior_mean']} | "
                f"{t['change']:+} ({_fmt(t['pct_change'], '%')}) | {t['direction']}{verdict} | {readings} |"
            )
        lines.append('')
        lines.append('"Direction" only fires when the shift exceeds a quarter of the metric\'s own '
                     'standard deviation, so day-to-day noise is not reported as a trend. '
                     '"Readings" counts genuine measurements in each period, not filled-in rows.')
        lines.append('')

    if withheld_trends:
        lines.extend(['## Trends deliberately not reported', ''])
        for w in withheld_trends:
            lines.append(f"- `{w['metric']}` — {w['reason']}")
        lines.append('')
        lines.append('These are measured occasionally rather than daily. Averaging a carried-forward '
                     'value would turn one or two readings into a confident-looking multi-week trend, '
                     'so the comparison is withheld instead. Note that switching scales or apps also '
                     'shifts these values independently of any real change in your body.')
        lines.append('')

    # --- contrasts
    if contrasts:
        lines.extend(['## What actually moves your numbers', '',
                      '| Comparison | Below threshold | At or above | Difference |',
                      '|---|---|---|---|'])
        for c in contrasts:
            lines.append(
                f"| {c['driver']} < {c['threshold']:g} vs >= — effect on {c['outcome']} "
                f"(+{c['lag_days']}d) | {c['low_mean']} (n={c['low_n']}) | "
                f"{c['high_mean']} (n={c['high_n']}) | {c['difference']:+} |"
            )
        lines.append('')

    # --- associations
    if associations:
        lines.extend(['## Associations', '',
                      '| Relationship | n | r | notable |', '|---|---|---|---|'])
        for a in associations:
            lines.append(f"| {a['label']} | {a['n']} | {a['r']:+} | {'yes' if a['notable'] else ''} |")
        lines.append('')
        lines.append('`r` is a correlation, not proof of cause, and "notable" only means the pattern is '
                     'unlikely to be pure chance (|t| > 2). Relationships with fewer than '
                     f'{MIN_PAIRS} paired days are omitted rather than reported weakly.')
        lines.append('')

    # --- coverage caveats
    thin = [c for c in coverage
            if c['sparse_by_design'] == 'no'
            and c['coverage_pct_since_reliable_start'] not in ('', None)
            and float(c['coverage_pct_since_reliable_start']) < 60]
    if thin:
        lines.extend(['## Metrics too thin to lean on', ''])
        for c in sorted(thin, key=lambda x: float(x['coverage_pct_since_reliable_start'])):
            lines.append(f"- `{c['column']}` — only {c['coverage_pct_since_reliable_start']}% of days "
                         f"covered since {c['reliable_start']}")
        lines.append('')

    lines.extend([
        '---',
        '',
        'Generated from your own Apple Health export. Every baseline is personal, not a population '
        'norm. This is a data summary, not medical advice — anything here that concerns you is a '
        'reason to talk to a clinician, not a diagnosis.',
    ])
    return '\n'.join(lines) + '\n'


def render_llm_context(
    daily_rows: list[dict[str, Any]],
    insight_rows: list[dict[str, Any]],
    trends: list[dict[str, Any]],
    associations: list[dict[str, Any]],
    start: date | None,
    history: HistoryResult | None = None,
    thin_metrics: Sequence[str] = (),
    recent_days: int = 28,
) -> str:
    """A compact, pre-analysed brief to paste into an LLM.

    Pasting 600 rows of daily_metrics.csv burns context and invites the model to
    do arithmetic badly. This hands over the conclusions and the recent detail.

    Ordered by decision-relevance rather than by how the numbers were computed.
    The historical anchor comes first because without it every level in the file
    is uninterpretable: 10 min/day of exercise is unremarkable for one person and
    a collapse for another, and only this subject's own record says which.
    """
    history = history or HistoryResult()
    lines = ['# Health context pack', '']
    lines.append('Pre-computed from one person\'s Apple Health export. Rolling baselines are their '
                 'own 60-day history; records and percentiles are their own whole record. Do not '
                 're-derive these numbers, reason about them. Sections are ordered most to least '
                 'decision-relevant. Read "What this data cannot tell you" at the end before '
                 'drawing conclusions.')
    lines.append('')

    if start:
        lines.append(f'- Valid analysis window begins: {start.isoformat()} '
                     '(first date on which resting HR, HRV and staged sleep are all live)')
    if insight_rows:
        lines.append(f'- Days analysed: {len(insight_rows)}')
        full = sum(1 for r in insight_rows if r.get('wear_class') == 'full')
        lines.append(f'- Days with full watch wear: {full}')
    lines.append('')

    lines.extend(health_history.render_situation(history))
    lines.extend(health_history.render_capacity(history))
    lines.extend(health_history.render_records(history))
    lines.extend(health_history.render_eras(history))
    lines.extend(health_history.render_yearly(history))
    lines.extend(health_history.render_cycles(history))
    lines.extend(health_history.render_distributions(history))
    lines.extend(health_history.render_streaks(history))

    if trends:
        lines.append('## Direction of travel (last 30d vs prior 90d)')
        lines.append('')
        lines.append('Short-horizon only. Both periods sit inside the current era, so a "flat" '
                     'reading here can still be far below the capacity table above.')
        lines.append('')
        for t in trends:
            verdict = f", {t['verdict']}" if t['verdict'] else ''
            lines.append(f"- {t['metric']}: {t['prior_mean']} -> {t['recent_mean']} {t['unit']} "
                         f"({t['change']:+}{verdict})")
        lines.append('')

    notable = [a for a in associations if a['notable']]
    if notable:
        lines.append('## Statistically notable personal associations')
        lines.append('')
        lines.append(f'Correlations over paired days (min {MIN_PAIRS} pairs, |t| > 2). '
                     'Association, not cause.')
        lines.append('')
        for a in notable:
            lines.append(f"- {a['label']}: r={a['r']:+} (n={a['n']}, lag {a['lag_days']}d)")
        lines.append('')

    lines.extend(health_history.render_episodes(history))

    recent = insight_rows[-recent_days:]
    if recent:
        lines.append(f'## Last {len(recent)} day{"s" if len(recent) != 1 else ""} (detail)')
        lines.append('')
        lines.append('Units: recovery 0-100 (70 = at baseline), HRV z and RHR dev are vs the '
                     '60-day personal baseline, sleep/deep/REM in hours, load ratio is 7d:28d '
                     'exercise minutes. `n/a` means not measured.')
        lines.append('')
        lines.append('| date | wear | recovery | HRV z | RHR dev | sleep h | deep h | REM h | '
                     'exercise min | load ratio |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|')
        daily_by_date = {r['date']: r for r in daily_rows}
        for r in recent:
            d = daily_by_date.get(r['date'], {})
            lines.append(
                f"| {r['date']} | {r.get('wear_class', '')} | {_fmt(r.get('recovery_score'))} | "
                f"{_fmt(r.get('hrv_sdnn_z'))} | {_fmt(r.get('resting_hr_dev'))} | "
                f"{_fmt(d.get('sleep_asleep_hours'))} | {_fmt(d.get('sleep_deep_hours'))} | "
                f"{_fmt(d.get('sleep_rem_hours'))} | {_fmt(d.get('exercise_minutes'))} | "
                f"{_fmt(r.get('load_ratio'))} |"
            )
        lines.append('')

    lines.extend(health_history.render_limits(history, thin_metrics))

    lines.extend([
        '## How to use this',
        '',
        'Anchor every judgement on the capacity table, not on the 30d-vs-90d trends: the trends '
        'compare two points inside the same regime and will call a sustained collapse "flat". '
        'Name the two or three changes with the largest expected effect, size them against what '
        'this person has already proven they can hold, and say explicitly when the data cannot '
        'answer a question. Blank or `n/a` cells mean "not measured", never zero.',
    ])
    return '\n'.join(lines) + '\n'


def write_insight_outputs(
    base_dir: str,
    daily_rows: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    start: date | None,
) -> dict[str, str]:
    insight_rows = build_daily_insights(daily_rows, start)
    trends, withheld_trends = compute_trends(daily_rows, start)
    associations = analyse_associations(daily_rows, start)
    contrasts = [
        c for c in (
            threshold_contrast(daily_rows, driver, outcome, threshold, lag, start)
            for driver, outcome, threshold, lag in CONTRASTS
        ) if c
    ]
    history = health_history.build_history(daily_rows, insight_rows, start)
    thin_metrics = thinly_covered_metrics(coverage)

    paths = {
        'daily_insights': os.path.join(base_dir, 'daily_insights.csv'),
        'insights_report': os.path.join(base_dir, 'insights_report.md'),
        'llm_context': os.path.join(base_dir, 'llm_context.md'),
    }

    with open(paths['daily_insights'], 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_INSIGHT_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(insight_rows)

    with open(paths['insights_report'], 'w', encoding='utf-8') as f:
        f.write(render_insights_report(insight_rows, trends, associations,
                                       contrasts, start, coverage, withheld_trends, history))

    with open(paths['llm_context'], 'w', encoding='utf-8') as f:
        f.write(render_llm_context(daily_rows, insight_rows, trends, associations, start,
                                   history, thin_metrics))

    return paths
