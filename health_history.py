"""Long-memory context: personal records, capacity gaps, eras, streaks, episodes.

A rolling 60-day baseline answers "is today normal for me *lately*". It cannot
answer "is my current level good or catastrophic for me", because after a long
decline the baseline quietly moves down with you — comparing the last 30 days to
the prior 90 measures the slope of a collapse from inside it, and reports a mild
worsening. This module holds the view the baselines cannot hold: what this
person has actually demonstrated they can do, when they did it, and how far
today sits from that.

Everything here is descriptive. It says "you held 69 min/day for 28 days in
Feb 2025 and you are at 11 min/day now"; it never claims to know why.

Deliberately imports nothing from health_insights: that module imports this one
in order to render its reports, and the reverse edge would close a cycle.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CAPACITY_WINDOW = 28      # days in the "sustained capacity" window
SHORT_WINDOW = 7          # days in the short rolling-record window

# Shortest period that may hold a record at that scope. Without these a single
# in-window day of a truncated first week could out-rank a real week.
MIN_WEEK_DAYS = 5
MIN_MONTH_DAYS = 20

# Two flagged days either side of a clear gap this size or smaller are the same
# episode. Illness signals routinely drop out for a day mid-episode.
EPISODE_MAX_GAP_DAYS = 2

# An era shorter than this is noise around a band boundary, not a regime.
MIN_ERA_DAYS = 21

# Half-width of the centred window used to segment eras. Centred, not trailing:
# a trailing mean puts every boundary ~4 weeks after the behaviour actually
# changed, which misdates exactly the thing this section exists to date.
ERA_SMOOTH_HALF_WIDTH = 14

EPS = 1e-9


@dataclass(frozen=True)
class HistoryMetric:
    """A metric worth remembering the all-time shape of.

    kind='sum' is a quantity accumulated over the day (exercise minutes, steps);
    kind='level' is a reading taken at a moment (HRV, weight, VO2 max). The
    distinction decides whether a blank day means zero or means unknown.
    """

    column: str
    label: str
    unit: str
    kind: str
    higher_is_better: bool = True
    round_to: int = 1
    # Fraction of a window's days that must carry a value before the window is
    # allowed to hold a mean at all.
    min_coverage: float = 0.6


KEY_METRICS: list[HistoryMetric] = [
    HistoryMetric('exercise_minutes', 'Exercise', 'min/day', 'sum', round_to=1),
    HistoryMetric('effort_vigorous_min', 'Vigorous effort', 'min/day', 'sum', round_to=1),
    HistoryMetric('steps', 'Steps', 'steps/day', 'sum', round_to=0),
    HistoryMetric('active_kcal', 'Active energy', 'kcal/day', 'sum', round_to=0),
    HistoryMetric('daylight_minutes', 'Daylight', 'min/day', 'sum', round_to=0),
    HistoryMetric('sleep_asleep_hours', 'Sleep', 'h/night', 'level', round_to=2),
    HistoryMetric('sleep_deep_hours', 'Deep sleep', 'h/night', 'level', round_to=2),
    HistoryMetric('hrv_sdnn', 'HRV (SDNN)', 'ms', 'level', round_to=1),
    HistoryMetric('resting_hr', 'Resting HR', 'bpm', 'level', higher_is_better=False, round_to=1),
    # Apple emits VO2 max only after qualifying outdoor walks/runs, so a 28-day
    # window holding two readings is normal and still worth reporting.
    HistoryMetric('vo2max', 'VO2 max', 'ml/kg/min', 'level', round_to=1, min_coverage=0.05),
]

METRICS_BY_COLUMN = {m.column: m for m in KEY_METRICS}

# The load metric eras are cut on, and the metrics described alongside each era.
ERA_LOAD_COLUMN = 'exercise_minutes'
ERA_CONTEXT_METRICS: list[HistoryMetric] = [
    METRICS_BY_COLUMN['sleep_asleep_hours'],
    METRICS_BY_COLUMN['hrv_sdnn'],
    METRICS_BY_COLUMN['resting_hr'],
    HistoryMetric('body_mass_kg', 'Weight', 'kg', 'level', round_to=1, min_coverage=0.02),
]

# Upper bound (exclusive) of each load band, in minutes/day of exercise.
ERA_BANDS: list[tuple[float, str]] = [
    (10.0, 'dormant'),
    (30.0, 'light'),
    (60.0, 'active'),
    (math.inf, 'peak'),
]

ERA_BAND_LEGEND = 'dormant <10, light 10-29, active 30-59, peak >=60 min/day of exercise'


@dataclass(frozen=True)
class StreakRule:
    column: str
    threshold: float
    label: str


STREAK_RULES: list[StreakRule] = [
    StreakRule('exercise_minutes', 30.0, 'days with >=30 min exercise'),
    StreakRule('steps', 8000.0, 'days with >=8,000 steps'),
    StreakRule('sleep_asleep_hours', 7.0, 'nights with >=7 h sleep'),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def to_float(value: Any) -> float | None:
    if value in ('', None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in 0..100."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def is_better(a: float, b: float, higher_is_better: bool) -> bool:
    return a > b + EPS if higher_is_better else a < b - EPS


def iso_week_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f'{y}-W{w:02d}'


def date_span_label(start: date, end: date) -> str:
    return start.isoformat() if start == end else f'{start.isoformat()}..{end.isoformat()}'


def fmt(value: float | None, digits: int = 1) -> str:
    """Numbers in these reports are read by a model that cannot see the source,
    so a blank must never be renderable as a zero."""
    if value is None:
        return 'n/a'
    if digits <= 0:
        return f'{value:,.0f}'
    return f'{value:.{digits}f}'


# ---------------------------------------------------------------------------
# Series construction
# ---------------------------------------------------------------------------

def build_metric_series(
    rows_by_date: dict[date, dict[str, Any]],
    metric: HistoryMetric,
    days: Sequence[date],
) -> dict[date, float]:
    """Daily values for one metric, with cumulative metrics zero-filled.

    A day that has a daily row but no AppleExerciseTime samples genuinely is
    zero exercise, and dropping it would let a month of inactivity masquerade as
    a month of missing data. A day with no row at all stays missing. Levels are
    never zero-filled: an unmeasured morning is not a zero-millisecond HRV.
    """
    out: dict[date, float] = {}
    for d in days:
        row = rows_by_date.get(d)
        if row is None:
            continue
        value = to_float(row.get(metric.column))
        if value is None and metric.kind == 'sum':
            value = 0.0
        if value is not None:
            out[d] = value
    return out


def required_observations(metric: HistoryMetric, span: int) -> int:
    """Observations a period must carry before it may hold a mean.

    A multi-day period needs at least two, whatever the coverage floor says:
    with one reading a "best 7 days" for VO2 max is just the best day wearing a
    wider label, and it would tie with every window that happened to contain it.
    """
    floor = 1 if span <= 1 else 2
    return max(floor, math.ceil(metric.min_coverage * span))


def aggregate(
    series: dict[date, float],
    period_days: Sequence[date],
    metric: HistoryMetric,
) -> tuple[float, float, int] | None:
    """(mean per day, total, observations) over a set of days, or None if thin."""
    values = [series[d] for d in period_days if d in series]
    if len(values) < required_observations(metric, len(period_days)):
        return None
    return statistics.mean(values), sum(values), len(values)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordEntry:
    metric: str
    scope: str            # 'day' | '7d' | '28d' | 'week' | 'month'
    per_day: float        # mean per day, comparable across every scope
    total: float | None   # window total; only meaningful for cumulative metrics
    period: str
    observations: int
    ties: int             # how many other periods equalled this value


def _best(
    candidates: Sequence[tuple[float, float | None, str, int]],
    higher_is_better: bool,
) -> tuple[float, float | None, str, int, int] | None:
    """Pick the best candidate, counting ties. Earliest period wins a tie so the
    output is stable between runs and the *first* time a level was reached is
    the one reported."""
    if not candidates:
        return None
    best = candidates[0]
    for cand in candidates[1:]:
        if is_better(cand[0], best[0], higher_is_better):
            best = cand
    ties = sum(1 for c in candidates if abs(c[0] - best[0]) <= EPS) - 1
    return best[0], best[1], best[2], best[3], ties


def compute_records(
    series: dict[date, float],
    days: Sequence[date],
    metric: HistoryMetric,
) -> list[RecordEntry]:
    """Best single day, best rolling 7d/28d, best ISO week, best calendar month."""
    entries: list[RecordEntry] = []
    if not series:
        return entries

    day_cands = [(v, v, d.isoformat(), 1) for d, v in sorted(series.items())]
    scopes: list[tuple[str, list[tuple[float, float | None, str, int]]]] = [('day', day_cands)]

    for scope, width in (('7d', SHORT_WINDOW), ('28d', CAPACITY_WINDOW)):
        cands: list[tuple[float, float | None, str, int]] = []
        for end in range(width - 1, len(days)):
            block = days[end - width + 1:end + 1]
            agg = aggregate(series, block, metric)
            if agg:
                cands.append((agg[0], agg[1], date_span_label(block[0], block[-1]), agg[2]))
        scopes.append((scope, cands))

    for scope, key, min_days in (
        ('week', iso_week_label, MIN_WEEK_DAYS),
        ('month', lambda d: d.strftime('%Y-%m'), MIN_MONTH_DAYS),
    ):
        buckets: dict[str, list[date]] = defaultdict(list)
        for d in days:
            buckets[key(d)].append(d)
        cands = []
        for label, block in sorted(buckets.items()):
            if len(block) < min_days:
                continue
            agg = aggregate(series, block, metric)
            if agg:
                cands.append((agg[0], agg[1], label, agg[2]))
        scopes.append((scope, cands))

    for scope, cands in scopes:
        won = _best(cands, metric.higher_is_better)
        if won:
            per_day, total, period, obs, ties = won
            entries.append(RecordEntry(metric.column, scope, per_day, total, period, obs, ties))
    return entries


# ---------------------------------------------------------------------------
# Capacity gap and distributions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapacityRow:
    metric: str
    label: str
    unit: str
    higher_is_better: bool
    round_to: int
    current: float | None
    current_period: str
    best: float | None
    best_period: str
    pct_of_peak: float | None
    percentile: float | None   # rank of current among all 28-day windows, 0-100
    windows: int


@dataclass(frozen=True)
class Distribution:
    metric: str
    label: str
    unit: str
    n_days: int
    p10: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p90: float | None
    lowest: float | None
    highest: float | None
    round_to: int


def rolling_window_means(
    series: dict[date, float],
    days: Sequence[date],
    metric: HistoryMetric,
    width: int = CAPACITY_WINDOW,
) -> list[tuple[float, date, date]]:
    out: list[tuple[float, date, date]] = []
    for end in range(width - 1, len(days)):
        block = days[end - width + 1:end + 1]
        agg = aggregate(series, block, metric)
        if agg:
            out.append((agg[0], block[0], block[-1]))
    return out


def compute_capacity(
    series: dict[date, float],
    days: Sequence[date],
    metric: HistoryMetric,
) -> CapacityRow:
    """Current sustained level against the best this person has ever sustained.

    The window is 28 days on both sides deliberately: a single heroic day proves
    nothing about capacity, and a four-week block is long enough that holding it
    required the sleep, time and motivation to actually be there.
    """
    windows = rolling_window_means(series, days, metric)

    # The current window is the trailing 28 days even when the record is shorter
    # than that; a 2-day export should still describe those 2 days.
    tail = days[-CAPACITY_WINDOW:]
    current_agg = aggregate(series, tail, metric) if tail else None
    current = current_agg[0] if current_agg else None
    current_period = date_span_label(tail[0], tail[-1]) if tail else ''

    best_value: float | None = None
    best_period = ''
    for value, w_start, w_end in windows:
        if best_value is None or is_better(value, best_value, metric.higher_is_better):
            best_value, best_period = value, date_span_label(w_start, w_end)

    pct: float | None = None
    if current is not None and best_value is not None:
        # For lower-is-better metrics the ratio is inverted so that 100% always
        # means "at your proven best" and less always means worse.
        num, den = (current, best_value) if metric.higher_is_better else (best_value, current)
        if den > EPS:
            pct = 100.0 * num / den

    rank: float | None = None
    if current is not None and windows:
        at_or_below = sum(
            1 for v, _s, _e in windows
            if not is_better(v, current, metric.higher_is_better)
        )
        rank = 100.0 * at_or_below / len(windows)

    return CapacityRow(
        metric=metric.column,
        label=metric.label,
        unit=metric.unit,
        higher_is_better=metric.higher_is_better,
        round_to=metric.round_to,
        current=current,
        current_period=current_period,
        best=best_value,
        best_period=best_period,
        pct_of_peak=pct,
        percentile=rank,
        windows=len(windows),
    )


def compute_distribution(series: dict[date, float], metric: HistoryMetric) -> Distribution:
    values = list(series.values())
    return Distribution(
        metric=metric.column,
        label=metric.label,
        unit=metric.unit,
        n_days=len(values),
        p10=percentile(values, 10),
        p25=percentile(values, 25),
        p50=percentile(values, 50),
        p75=percentile(values, 75),
        p90=percentile(values, 90),
        lowest=min(values) if values else None,
        highest=max(values) if values else None,
        round_to=metric.round_to,
    )


# ---------------------------------------------------------------------------
# Eras
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Era:
    start: date
    end: date
    days: int
    band: str
    load_mean: float | None
    context: dict[str, float | None]


def band_for(load: float) -> str:
    for upper, name in ERA_BANDS:
        if load < upper:
            return name
    return ERA_BANDS[-1][1]


def centred_means(
    series: dict[date, float],
    days: Sequence[date],
    half_width: int = ERA_SMOOTH_HALF_WIDTH,
) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(days)):
        block = days[max(0, i - half_width):min(len(days), i + half_width + 1)]
        values = [series[d] for d in block if d in series]
        out.append(statistics.mean(values) if values else None)
    return out


def segment_eras(
    load_series: dict[date, float],
    days: Sequence[date],
    context_series: dict[str, dict[date, float]],
    min_era_days: int = MIN_ERA_DAYS,
) -> list[Era]:
    """Cut the timeline into contiguous stretches of one load band.

    The rule is deliberately one a human can check by eye: smooth the load with
    a centred 28-day mean, drop each day into a fixed band, merge neighbouring
    days that share a band, then absorb any stretch shorter than three weeks
    into whichever neighbour is longer. No fitted parameters, no hidden state.
    """
    if not days:
        return []
    smoothed = centred_means(load_series, days)
    bands: list[str | None] = [band_for(v) if v is not None else None for v in smoothed]
    if all(b is None for b in bands):
        return []

    # Stretches with no load data at all inherit the nearest classified day
    # rather than becoming an era of their own.
    for i in range(1, len(bands)):
        if bands[i] is None:
            bands[i] = bands[i - 1]
    for i in range(len(bands) - 2, -1, -1):
        if bands[i] is None:
            bands[i] = bands[i + 1]

    segments: list[list[Any]] = []  # [start_index, end_index, band]
    for i, b in enumerate(bands):
        if segments and segments[-1][2] == b:
            segments[-1][1] = i
        else:
            segments.append([i, i, b])

    while len(segments) > 1:
        shortest = min(range(len(segments)), key=lambda i: segments[i][1] - segments[i][0] + 1)
        if segments[shortest][1] - segments[shortest][0] + 1 >= min_era_days:
            break
        has_prev = shortest > 0
        has_next = shortest + 1 < len(segments)
        prev_len = (segments[shortest - 1][1] - segments[shortest - 1][0] + 1) if has_prev else -1
        next_len = (segments[shortest + 1][1] - segments[shortest + 1][0] + 1) if has_next else -1
        if has_prev and prev_len >= next_len:
            segments[shortest - 1][1] = segments[shortest][1]
        else:
            segments[shortest + 1][0] = segments[shortest][0]
        segments.pop(shortest)

    # Label from the measured mean rather than the smoothed band, so the band
    # printed next to a number always matches that number — then re-coalesce,
    # because absorbing a short stretch (or relabelling) can leave two
    # neighbours sharing a band, and "two adjacent identical eras" is not a
    # thing the eras table should ever show.
    def band_of(start_i: int, end_i: int) -> str:
        loads = [load_series[d] for d in days[start_i:end_i + 1] if d in load_series]
        return band_for(statistics.mean(loads)) if loads else 'unknown'

    merged = True
    while merged and len(segments) > 1:
        merged = False
        for i in range(len(segments) - 1):
            if band_of(*segments[i][:2]) == band_of(*segments[i + 1][:2]):
                segments[i][1] = segments[i + 1][1]
                segments.pop(i + 1)
                merged = True
                break

    eras: list[Era] = []
    for start_i, end_i, _band in segments:
        block = days[start_i:end_i + 1]
        loads = [load_series[d] for d in block if d in load_series]
        load_mean = statistics.mean(loads) if loads else None
        context: dict[str, float | None] = {}
        for column, series in context_series.items():
            vals = [series[d] for d in block if d in series]
            context[column] = statistics.mean(vals) if vals else None
        eras.append(Era(
            start=block[0],
            end=block[-1],
            days=len(block),
            band=band_for(load_mean) if load_mean is not None else 'unknown',
            load_mean=load_mean,
            context=context,
        ))
    return eras


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Streak:
    label: str
    column: str
    threshold: float
    days_met: int
    total_days: int
    current: int
    longest: int
    longest_start: date | None
    longest_end: date | None


def compute_streak(
    series: dict[date, float],
    days: Sequence[date],
    rule: StreakRule,
) -> Streak:
    """Longest and current run of days meeting a threshold.

    A day with no value breaks the streak. That is the conservative reading: an
    unrecorded night is not evidence of seven hours' sleep.
    """
    longest = current = 0
    longest_end_i = -1
    days_met = 0
    for i, d in enumerate(days):
        value = series.get(d)
        if value is not None and value >= rule.threshold:
            current += 1
            days_met += 1
            if current > longest:
                longest, longest_end_i = current, i
        else:
            current = 0
    longest_start = days[longest_end_i - longest + 1] if longest_end_i >= 0 else None
    longest_end = days[longest_end_i] if longest_end_i >= 0 else None
    return Streak(
        label=rule.label,
        column=rule.column,
        threshold=rule.threshold,
        days_met=days_met,
        total_days=len(days),
        current=current,
        longest=longest,
        longest_start=longest_start,
        longest_end=longest_end,
    )


# ---------------------------------------------------------------------------
# Strain episodes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrainEpisode:
    start: date
    end: date
    span_days: int
    flagged_days: int
    peak_signals: int
    signals: list[str]


def signal_kind(text: str) -> str:
    """Reduce 'wrist temp +0.45degC' to 'wrist temp'.

    Matches on shape rather than on a hard-coded vocabulary, so a reworded
    signal in health_insights degrades into a slightly longer label instead of
    silently disappearing from every episode.
    """
    words: list[str] = []
    for word in text.split():
        if word[0].isdigit() or word[0] in '+-':
            break
        words.append(word)
    return ' '.join(words) or text.strip()


def group_strain_episodes(
    insight_rows: Sequence[dict[str, Any]],
    max_gap_days: int = EPISODE_MAX_GAP_DAYS,
) -> list[StrainEpisode]:
    """Collapse scattered flagged days into episodes.

    Sixty loose dates read as noise; eight episodes with durations read as a
    history. Signals routinely drop below threshold for a day mid-illness, so a
    short clear gap does not end the episode.
    """
    flagged = []
    for row in insight_rows:
        if row.get('strain_flag') != 'yes':
            continue
        d = date.fromisoformat(row['date'])
        count = int(to_float(row.get('strain_signal_count')) or 0)
        kinds = [signal_kind(s) for s in str(row.get('strain_detail', '')).split(';') if s.strip()]
        flagged.append((d, count, kinds))
    flagged.sort()

    episodes: list[StrainEpisode] = []
    group: list[tuple[date, int, list[str]]] = []

    def flush() -> None:
        if not group:
            return
        seen: list[str] = []
        for _d, _c, kinds in group:
            for k in kinds:
                if k not in seen:
                    seen.append(k)
        episodes.append(StrainEpisode(
            start=group[0][0],
            end=group[-1][0],
            span_days=(group[-1][0] - group[0][0]).days + 1,
            flagged_days=len(group),
            peak_signals=max(c for _d, c, _k in group),
            signals=seen,
        ))

    for item in flagged:
        if group and (item[0] - group[-1][0]).days - 1 > max_gap_days:
            flush()
            group = []
        group.append(item)
    flush()
    return episodes


# ---------------------------------------------------------------------------
# Seasonality, weekday shape, year over year
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bucket:
    label: str
    days: int
    values: dict[str, float | None]


def bucket_means(
    series_by_metric: dict[str, dict[date, float]],
    days: Sequence[date],
    key: Callable[[date], str],
    order: Callable[[str], Any] | None = None,
) -> list[Bucket]:
    grouped: dict[str, list[date]] = defaultdict(list)
    for d in days:
        grouped[key(d)].append(d)
    out: list[Bucket] = []
    for label in sorted(grouped, key=order or (lambda x: x)):
        block = grouped[label]
        values: dict[str, float | None] = {}
        for column, series in series_by_metric.items():
            vals = [series[d] for d in block if d in series]
            values[column] = statistics.mean(vals) if vals else None
        out.append(Bucket(label=label, days=len(block), values=values))
    return out


WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

@dataclass
class HistoryResult:
    first_day: date | None = None
    last_day: date | None = None
    n_days: int = 0
    days_with_rows: int = 0
    capacity: list[CapacityRow] = field(default_factory=list)
    records: dict[str, list[RecordEntry]] = field(default_factory=dict)
    distributions: list[Distribution] = field(default_factory=list)
    eras: list[Era] = field(default_factory=list)
    streaks: list[Streak] = field(default_factory=list)
    episodes: list[StrainEpisode] = field(default_factory=list)
    by_month: list[Bucket] = field(default_factory=list)
    by_weekday: list[Bucket] = field(default_factory=list)
    by_year: list[Bucket] = field(default_factory=list)
    calendar_years: int = 0


def build_history(
    daily_rows: Sequence[dict[str, Any]],
    insight_rows: Sequence[dict[str, Any]],
    start: date | None = None,
) -> HistoryResult:
    rows_by_date: dict[date, dict[str, Any]] = {}
    for row in daily_rows:
        if not row.get('date'):
            continue
        d = date.fromisoformat(row['date'])
        if start and d < start:
            continue
        rows_by_date[d] = row

    result = HistoryResult()
    if not rows_by_date:
        return result

    first, last = min(rows_by_date), max(rows_by_date)
    # A dense calendar, not just the days that produced rows: a gap in the
    # export is a gap in the person's history and windows must feel it.
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]

    result.first_day, result.last_day = first, last
    result.n_days = len(days)
    result.days_with_rows = len(rows_by_date)

    all_metrics = list(KEY_METRICS) + [m for m in ERA_CONTEXT_METRICS if m.column not in METRICS_BY_COLUMN]
    series_by_column = {m.column: build_metric_series(rows_by_date, m, days) for m in all_metrics}

    for metric in KEY_METRICS:
        series = series_by_column[metric.column]
        if not series:
            continue
        result.capacity.append(compute_capacity(series, days, metric))
        records = compute_records(series, days, metric)
        if records:
            result.records[metric.column] = records
        result.distributions.append(compute_distribution(series, metric))

    result.eras = segment_eras(
        series_by_column.get(ERA_LOAD_COLUMN, {}),
        days,
        {m.column: series_by_column[m.column] for m in ERA_CONTEXT_METRICS},
    )

    for rule in STREAK_RULES:
        series = series_by_column.get(rule.column)
        if series:
            result.streaks.append(compute_streak(series, days, rule))

    result.episodes = group_strain_episodes(insight_rows)

    seasonal = {c: series_by_column[c] for c in
                ('exercise_minutes', 'steps', 'sleep_asleep_hours', 'daylight_minutes', 'resting_hr')
                if c in series_by_column}
    result.by_month = bucket_means(seasonal, days, lambda d: MONTH_NAMES[d.month - 1],
                                   order=MONTH_NAMES.index)
    result.by_weekday = bucket_means(seasonal, days, lambda d: WEEKDAY_NAMES[d.weekday()],
                                     order=WEEKDAY_NAMES.index)
    yearly = {m.column: series_by_column[m.column] for m in KEY_METRICS}
    result.by_year = bucket_means(yearly, days, lambda d: str(d.year))
    result.calendar_years = len({d.year for d in days})
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(value: float | None) -> str:
    return 'n/a' if value is None else f'{value:.0f}%'


def render_situation(history: HistoryResult) -> list[str]:
    """One paragraph naming the largest change in the record, with dates."""
    load = next((c for c in history.capacity if c.metric == ERA_LOAD_COLUMN), None)
    if load is None or load.current is None or load.best is None:
        return []

    window = ''
    if history.first_day and history.last_day:
        window = (f'{history.first_day.isoformat()} to {history.last_day.isoformat()} '
                  f'({history.n_days} days)')

    parts = [
        f'Record covers {window}.' if window else '',
        f'Sustained exercise load is now **{fmt(load.current, 1)} min/day** '
        f'(mean of {load.current_period}) against a proven best of '
        f'**{fmt(load.best, 1)} min/day** held across the 28 days {load.best_period} — '
        f'**{_pct(load.pct_of_peak)} of demonstrated capacity**, ranking at the '
        f'{load.percentile:.0f}th percentile of the {load.windows} 28-day windows on record.',
    ]

    week = next((r for r in history.records.get(ERA_LOAD_COLUMN, []) if r.scope == 'week'), None)
    if week and week.total is not None:
        parts.append(f'Best single week ever: **{fmt(week.total, 0)} min in {week.period}** '
                     f'({fmt(week.per_day, 1)} min/day).')

    if len(history.eras) >= 2:
        current_era, prior = history.eras[-1], history.eras[-2]
        parts.append(
            f'The current regime ("{current_era.band}", {fmt(current_era.load_mean, 1)} min/day) '
            f'began {current_era.start.isoformat()} and has run {current_era.days} days; '
            f'it follows a "{prior.band}" stretch of {prior.days} days at '
            f'{fmt(prior.load_mean, 1)} min/day.')

    return ['## Situation', '', ' '.join(p for p in parts if p), '']


def render_capacity(history: HistoryResult) -> list[str]:
    if not history.capacity:
        return []
    current_period = history.capacity[0].current_period
    lines = [
        '## Capacity gap — current vs proven best',
        '',
        f'`current 28d` is the mean over {current_period}. '
        'Both columns are 28-day means, so they are like-for-like: a four-week block is long '
        'enough that holding it required the time, sleep and motivation to actually be there. '
        '`% of peak` is current/best (inverted for metrics where lower is better), so 100% means '
        '"back at your own proven ceiling". `pctile` is where the current 28 days rank among '
        'every 28-day window in the record.',
        '',
        '| metric | current 28d | best 28d | when | % of peak | pctile | windows |',
        '|---|---|---|---|---|---|---|',
    ]
    for row in history.capacity:
        direction = '' if row.higher_is_better else ' (lower better)'
        lines.append(
            f'| {row.label} ({row.unit}){direction} | {fmt(row.current, row.round_to)} | '
            f'{fmt(row.best, row.round_to)} | {row.best_period or "n/a"} | '
            f'{_pct(row.pct_of_peak)} | {_pct(row.percentile)} | {row.windows} |'
        )
    lines.append('')
    return lines


SCOPE_LABELS = {'day': 'best day', '7d': 'best 7 days', '28d': 'best 28 days',
                'week': 'best ISO week', 'month': 'best month'}


def render_records(history: HistoryResult) -> list[str]:
    if not history.records:
        return []
    lines = [
        '## Personal records (with dates)',
        '',
        'All values are per-day means so scopes compare directly; `total` is the summed amount '
        'over the period for cumulative metrics. For resting HR the "record" is the lowest. '
        f'A week or month clipped by the edge of the record still qualifies once {MIN_WEEK_DAYS} '
        f'(resp. {MIN_MONTH_DAYS}) of its days are in window, so a clipped period can hold a '
        'per-day record while its `total` covers fewer days than the label suggests.',
        '',
        '| metric | scope | per day | total | period |',
        '|---|---|---|---|---|',
    ]
    for metric in KEY_METRICS:
        entries = history.records.get(metric.column)
        if not entries:
            continue
        for entry in entries:
            total = fmt(entry.total, 0) if metric.kind == 'sum' and entry.total is not None else '-'
            tie = f' (+{entry.ties} tied)' if entry.ties else ''
            lines.append(
                f'| {metric.label} ({metric.unit}) | {SCOPE_LABELS[entry.scope]} | '
                f'{fmt(entry.per_day, metric.round_to)} | {total} | {entry.period}{tie} |'
            )
    lines.append('')
    return lines


def render_eras(history: HistoryResult) -> list[str]:
    if not history.eras:
        return []
    lines = [
        '## Eras',
        '',
        f'Contiguous stretches sharing one load band ({ERA_BAND_LEGEND}), cut on a centred '
        '28-day mean of exercise minutes; stretches under 3 weeks are absorbed into the longer '
        'neighbour. The context columns are means over the same stretch — they are what else was '
        'true then, not causes.',
        '',
        '| period | days | band | exercise min/day | sleep h | HRV ms | resting HR | weight kg |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for era in history.eras:
        ctx = era.context
        lines.append(
            f'| {era.start.isoformat()}..{era.end.isoformat()} | {era.days} | {era.band} | '
            f'{fmt(era.load_mean, 1)} | {fmt(ctx.get("sleep_asleep_hours"), 2)} | '
            f'{fmt(ctx.get("hrv_sdnn"), 1)} | {fmt(ctx.get("resting_hr"), 1)} | '
            f'{fmt(ctx.get("body_mass_kg"), 1)} |'
        )
    lines.append('')
    return lines


def render_distributions(history: HistoryResult) -> list[str]:
    if not history.distributions:
        return []
    lines = [
        '## Distribution of daily values (whole record)',
        '',
        'Use this to read any single number in this file. Percentiles are over days that carry a '
        'value; for cumulative metrics (exercise, steps, energy, daylight) a recorded day with no '
        'samples counts as 0, for readings (sleep, HRV, resting HR, VO2 max) an unmeasured day is '
        'excluded rather than counted as zero.',
        '',
        '| metric | days | p10 | p25 | median | p75 | p90 | min | max |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for dist in history.distributions:
        r = dist.round_to
        lines.append(
            f'| {dist.label} ({dist.unit}) | {dist.n_days} | {fmt(dist.p10, r)} | '
            f'{fmt(dist.p25, r)} | {fmt(dist.p50, r)} | {fmt(dist.p75, r)} | '
            f'{fmt(dist.p90, r)} | {fmt(dist.lowest, r)} | {fmt(dist.highest, r)} |'
        )
    lines.append('')
    return lines


def render_streaks(history: HistoryResult) -> list[str]:
    if not history.streaks:
        return []
    lines = [
        '## Streaks and consistency',
        '',
        '| threshold | days met | of days | current streak | longest | longest ran |',
        '|---|---|---|---|---|---|',
    ]
    for s in history.streaks:
        span = (f'{s.longest_start.isoformat()}..{s.longest_end.isoformat()}'
                if s.longest_start and s.longest_end else 'n/a')
        lines.append(
            f'| {s.label} | {s.days_met} | {s.total_days} | {s.current} | {s.longest} | {span} |'
        )
    lines.append('')
    lines.append('A day with no recorded value breaks a streak, so these are floors, not estimates.')
    lines.append('')
    return lines


def render_episodes(history: HistoryResult, limit: int = 12) -> list[str]:
    if not history.episodes:
        return []
    shown = history.episodes[-limit:]
    lines = [
        '## Physiological strain episodes',
        '',
        f'{len(history.episodes)} episode(s) across the record; flagged days within '
        f'{EPISODE_MAX_GAP_DAYS} clear days of each other are one episode. A flagged day is one '
        'where two or more of wrist temperature, resting HR, HRV, respiratory rate and SpO2 moved '
        'the wrong way against the personal 60-day baseline together.',
        '',
        '| episode | span days | flagged days | peak signals | signals |',
        '|---|---|---|---|---|',
    ]
    for e in shown:
        period = e.start.isoformat() if e.start == e.end else f'{e.start.isoformat()}..{e.end.isoformat()}'
        lines.append(f'| {period} | {e.span_days} | {e.flagged_days} | {e.peak_signals} | '
                     f'{", ".join(e.signals)} |')
    if len(shown) < len(history.episodes):
        lines.append('')
        lines.append(f'(most recent {len(shown)} of {len(history.episodes)} shown)')
    lines.append('')
    return lines


SEASONAL_COLUMNS = [
    ('exercise_minutes', 'exercise min/day', 1),
    ('steps', 'steps/day', 0),
    ('sleep_asleep_hours', 'sleep h', 2),
    ('daylight_minutes', 'daylight min', 0),
    ('resting_hr', 'resting HR', 1),
]


def _bucket_table(buckets: list[Bucket], first_header: str) -> list[str]:
    header = f'| {first_header} | days | ' + ' | '.join(lbl for _c, lbl, _r in SEASONAL_COLUMNS) + ' |'
    sep = '|---' * (len(SEASONAL_COLUMNS) + 2) + '|'
    lines = [header, sep]
    for b in buckets:
        cells = ' | '.join(fmt(b.values.get(c), r) for c, _lbl, r in SEASONAL_COLUMNS)
        lines.append(f'| {b.label} | {b.days} | {cells} |')
    return lines


def render_cycles(history: HistoryResult) -> list[str]:
    if not history.by_weekday and not history.by_month:
        return []
    lines = ['## Weekly and seasonal shape', '']
    if history.by_weekday:
        lines.extend(_bucket_table(history.by_weekday, 'weekday'))
        lines.append('')
    if history.by_month:
        lines.extend(_bucket_table(history.by_month, 'month'))
        lines.append('')
        if history.calendar_years < 3:
            lines.append(f'Month-of-year rows pool only {history.calendar_years} calendar year(s), '
                         'so "season" and "what was going on that year" are not separable here. '
                         'Read them with the eras table, not as a seasonal effect.')
            lines.append('')
    return lines


def render_yearly(history: HistoryResult) -> list[str]:
    if not history.by_year:
        return []
    columns = [(m.column, f'{m.label} ({m.unit})', m.round_to) for m in KEY_METRICS]
    header = '| year | days | ' + ' | '.join(lbl for _c, lbl, _r in columns) + ' |'
    lines = ['## Year over year', '', header, '|---' * (len(columns) + 2) + '|']
    for b in history.by_year:
        cells = ' | '.join(fmt(b.values.get(c), r) for c, _lbl, r in columns)
        lines.append(f'| {b.label} | {b.days} | {cells} |')
    lines.append('')
    lines.append('Partial years at either end of the record are not annualised; the `days` column '
                 'says how much of each year is present.')
    lines.append('')
    return lines


def render_limits(history: HistoryResult, thin_metrics: Sequence[str] = ()) -> list[str]:
    """The section that stops a reader treating this file as more than it is."""
    lines = [
        '## What this data cannot tell you',
        '',
        '- **Nothing here is causal.** Eras, correlations and contrasts are descriptions of what '
        'co-occurred. A collapse in exercise sits next to whatever else was happening in that '
        'person\'s life, none of which is in this export.',
        '- **Absence is not zero.** Cumulative metrics count a recorded day with no samples as 0; '
        'a day with no row at all is excluded everywhere. Readings (HRV, sleep, weight) are never '
        'zero-filled.',
        '- **The watch is the instrument.** Days without wear look like inactive days. Check the '
        'wear column before reading a low value as a behaviour change.',
        '- **No context on why.** Injury, illness beyond what the vitals caught, work, travel, '
        'mood, medication, life events — none of it is recorded here. Do not infer motivation, '
        'discipline or intent from these numbers, and ask before assuming a cause.',
        '- **Records are records, not targets.** A proven peak was reached once under conditions '
        'that may no longer exist. It bounds what has been possible, not what is advisable now.',
        '- **Not clinical data.** Consumer-wearable estimates with device-specific bias; VO2 max '
        'and body composition especially. Nothing here diagnoses anything.',
    ]
    if history.calendar_years < 3:
        lines.append(f'- **Short history.** {history.n_days} day(s) across '
                     f'{history.calendar_years} calendar year(s): seasonal claims are unsupported.')
    if thin_metrics:
        lines.append('- **Thinly measured here:** ' + ', '.join(thin_metrics) +
                     ' — treat their percentiles and records as indicative only.')
    lines.append('')
    return lines


def render_history_highlights(history: HistoryResult) -> list[str]:
    """Compact capacity + records block for the human-facing report."""
    lines: list[str] = []
    lines.extend(render_capacity(history))
    lines.extend(render_records(history))
    lines.extend(render_eras(history))
    lines.extend(render_streaks(history))
    return lines
