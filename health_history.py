"""Long-memory context: records, capacity, load bands, workouts, inferred events.

A rolling 60-day baseline answers "is today normal for me *lately*". It cannot
answer "is my current level good or catastrophic for me", because after a long
decline the baseline quietly moves down with you — comparing the last 30 days to
the prior 90 measures the slope of a collapse from inside it, and reports a mild
worsening. This module holds the view the baselines cannot hold: what this
person has demonstrably done, when they did it, what kind of training it was,
and how far today sits from that.

Everything here is descriptive, and the descriptions are deliberately weak where
the data is weak. It reports that a modality stopped on a date and that a
never-before-seen one started eleven weeks later; it does not decide why. Cause
is something to ask the person about, and the pack says so.

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

# Two flagged days either side of a clear gap this size or smaller are the same
# episode. Illness signals routinely drop out for a day mid-episode.
EPISODE_MAX_GAP_DAYS = 2

# A load band shorter than this is absorbed into its longer neighbour. This is a
# smoothing choice, not a discovery: see segment_eras.
MIN_ERA_DAYS = 21

# Half-width of the centred window used to band the load series. Centred, not
# trailing: a trailing mean puts every boundary ~4 weeks after the level
# actually changed, which misdates exactly the thing this section exists to date.
ERA_SMOOTH_HALF_WIDTH = 14

# Workout-history detectors.
BLACKOUT_MIN_DAYS = 14           # a run of workout-free days worth reporting
MODALITY_MIN_SESSIONS = 15       # sessions before a modality's exit is notable
MODALITY_QUIET_DAYS = 120        # no session in this long = stopped, not paused
NEW_MODALITY_MIN_SESSIONS = 20   # sessions before an arrival is notable
NEW_MODALITY_MIN_OFFSET = 60     # days into the record before "new" means new

# Weekly progression step. 10% is the conventional conservative ramp; the
# acute:chronic literature puts the upper safe bound around 1.3.
PROGRESSION_STEP = 1.10
PROGRESSION_MAX_RATIO = 1.30

EPS = 1e-9


@dataclass(frozen=True)
class HistoryMetric:
    """A metric worth remembering the all-time shape of.

    kind='sum' is a quantity accumulated over the day (exercise minutes, steps);
    kind='level' is a reading taken at a moment (HRV, weight, VO2 max). The
    distinction decides whether a period's headline carries a total as well as a
    mean, and whether a ratio against the best-ever value means anything — see
    zero_floored.
    """

    column: str
    label: str
    unit: str
    kind: str
    higher_is_better: bool = True
    round_to: int = 1
    # Fraction of a period's days that must carry a measurement before the
    # period is allowed to hold a mean.
    min_coverage: float = 0.6

    @property
    def zero_floored(self) -> bool:
        """True when 0 is both attainable and meaningful for this metric.

        Only these support a "% of peak" ratio. Resting HR never approaches
        zero, so its ratio is trapped in a narrow band near 100% and a reader
        comparing it against exercise's ratio is comparing two different scales.
        """
        return self.kind == 'sum'


KEY_METRICS: list[HistoryMetric] = [
    HistoryMetric('exercise_minutes', 'Exercise', 'min/day', 'sum', round_to=1),
    HistoryMetric('effort_vigorous_min', 'Vigorous effort', 'min/day', 'sum', round_to=1),
    HistoryMetric('steps', 'Steps', 'steps/day', 'sum', round_to=0),
    HistoryMetric('active_kcal', 'Active energy', 'kcal/day', 'sum', round_to=0),
    HistoryMetric('daylight_minutes', 'Daylight', 'min/day', 'sum', round_to=0),
    HistoryMetric('sleep_asleep_hours', 'Sleep', 'h/night', 'level', round_to=2),
    HistoryMetric('sleep_deep_hours', 'Deep sleep', 'h/night', 'level', round_to=2),
    HistoryMetric('sleep_rem_hours', 'REM sleep', 'h/night', 'level', round_to=2),
    HistoryMetric('sleep_deep_pct', 'Deep sleep share', '% of sleep', 'level', round_to=1),
    HistoryMetric('sleep_rem_pct', 'REM share', '% of sleep', 'level', round_to=1),
    HistoryMetric('hrv_sdnn', 'HRV (SDNN)', 'ms', 'level', round_to=1),
    HistoryMetric('resting_hr', 'Resting HR', 'bpm', 'level', higher_is_better=False, round_to=1),
    # Apple emits VO2 max only after qualifying outdoor walks/runs, so a 28-day
    # window holding two readings is normal and still worth reporting.
    HistoryMetric('vo2max', 'VO2 max', 'ml/kg/min', 'level', round_to=1, min_coverage=0.05),
]

METRICS_BY_COLUMN = {m.column: m for m in KEY_METRICS}

# The metric the load bands are cut on, and the metrics described alongside each.
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


def date_span_label(start: date, end: date) -> str:
    return start.isoformat() if start == end else f'{start.isoformat()}..{end.isoformat()}'


def fmt(value: float | None, digits: int = 1) -> str:
    """Numbers in these reports are read by a model that cannot see the source,
    so a missing value must never be renderable as a zero."""
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
    suspect: Sequence[tuple[str, date]] = (),
) -> dict[date, float]:
    """Measured values only. Nothing is imputed, in either direction.

    An earlier version zero-filled cumulative metrics on days that carried a row
    but no samples, on the theory that a recorded day with no AppleExerciseTime
    really is zero exercise. That theory manufactured data: it put 150 fabricated
    zeros into the daylight series, all of them concentrated in winter when the
    watch goes outdoors least, and reported January 2025 as 36 min/day when the
    days actually measured averaged 125. HealthKit does not distinguish "nothing
    happened" from "nothing was recorded", so neither does this — every period
    carries its measured-day count instead.
    """
    skip = {d for column, d in suspect if column == metric.column}
    out: dict[date, float] = {}
    for d in days:
        row = rows_by_date.get(d)
        if row is None or d in skip:
            continue
        value = to_float(row.get(metric.column))
        if value is not None:
            out[d] = value
    return out


def required_observations(metric: HistoryMetric, span: int) -> int:
    """Measurements a period must carry before it may hold a mean.

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
    """(mean per measured day, total, measured days) or None if too thin.

    The total sums only what was measured, so on a period with gaps it
    understates while the mean does not. Renderers print the measured-day count
    beside both.
    """
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
    scope: str            # 'day' | '7d' | '28d'
    per_day: float        # mean per measured day, comparable across scopes
    total: float | None   # summed measured amount; cumulative metrics only
    period: str
    observations: int
    span: int
    ties: int             # how many other periods equalled this value


def _best(
    candidates: Sequence[tuple[float, float | None, str, int, int]],
    higher_is_better: bool,
) -> tuple[float, float | None, str, int, int, int] | None:
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
    return (*best, ties)


def compute_records(
    series: dict[date, float],
    days: Sequence[date],
    metric: HistoryMetric,
) -> list[RecordEntry]:
    """Best single day, best rolling 7 days, best rolling 28 days.

    ISO-week and calendar-month scopes used to be here and were dropped: a best
    ISO week is a best rolling 7 days snapped to an arbitrary grid, and it
    reported the same week shifted by a day at the cost of a fifth of the pack.
    """
    entries: list[RecordEntry] = []
    if not series:
        return entries

    day_cands = [(v, v, d.isoformat(), 1, 1) for d, v in sorted(series.items())]
    scopes: list[tuple[str, list[tuple[float, float | None, str, int, int]]]] = [('day', day_cands)]

    for scope, width in (('7d', SHORT_WINDOW), ('28d', CAPACITY_WINDOW)):
        cands: list[tuple[float, float | None, str, int, int]] = []
        for end in range(width - 1, len(days)):
            block = days[end - width + 1:end + 1]
            agg = aggregate(series, block, metric)
            if agg:
                cands.append((agg[0], agg[1], date_span_label(block[0], block[-1]), agg[2], width))
        scopes.append((scope, cands))

    for scope, cands in scopes:
        won = _best(cands, metric.higher_is_better)
        if won:
            per_day, total, period, obs, span, ties = won
            entries.append(RecordEntry(metric.column, scope, per_day, total, period, obs, span, ties))
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
    zero_floored: bool
    round_to: int
    current: float | None
    current_period: str
    current_observed: int
    current_span: int
    best: float | None
    best_period: str
    best_observed: int
    pct_of_peak: float | None
    percentile: float | None   # rank of current among all 28-day windows, 0-100
    windows: int


@dataclass(frozen=True)
class Distribution:
    metric: str
    label: str
    unit: str
    n_days: int
    days_missing: int
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
) -> list[tuple[float, date, date, int]]:
    out: list[tuple[float, date, date, int]] = []
    for end in range(width - 1, len(days)):
        block = days[end - width + 1:end + 1]
        agg = aggregate(series, block, metric)
        if agg:
            out.append((agg[0], block[0], block[-1], agg[2]))
    return out


def compute_capacity(
    series: dict[date, float],
    days: Sequence[date],
    metric: HistoryMetric,
) -> CapacityRow:
    """Current sustained level against the best this person has ever sustained.

    The window is 28 days on both sides deliberately: a single heroic day proves
    nothing about capacity, while a four-week block holds through the ordinary
    interruptions of a month. Note what a 28-day mean cannot see — whether the
    minutes in it were training or rehabilitation. The modality table exists
    because this table cannot answer that.
    """
    windows = rolling_window_means(series, days, metric)

    # The current window is the trailing 28 days even when the record is shorter
    # than that; a 2-day export should still describe those 2 days.
    tail = days[-CAPACITY_WINDOW:]
    current_agg = aggregate(series, tail, metric) if tail else None
    current = current_agg[0] if current_agg else None

    best_value: float | None = None
    best_period = ''
    best_observed = 0
    for value, w_start, w_end, obs in windows:
        if best_value is None or is_better(value, best_value, metric.higher_is_better):
            best_value = value
            best_period = date_span_label(w_start, w_end)
            best_observed = obs

    pct: float | None = None
    if metric.zero_floored and current is not None and best_value is not None and best_value > EPS:
        pct = 100.0 * current / best_value

    rank: float | None = None
    if current is not None and windows:
        at_or_below = sum(
            1 for v, _s, _e, _n in windows
            if not is_better(v, current, metric.higher_is_better)
        )
        rank = 100.0 * at_or_below / len(windows)

    return CapacityRow(
        metric=metric.column,
        label=metric.label,
        unit=metric.unit,
        higher_is_better=metric.higher_is_better,
        zero_floored=metric.zero_floored,
        round_to=metric.round_to,
        current=current,
        current_period=date_span_label(tail[0], tail[-1]) if tail else '',
        current_observed=current_agg[2] if current_agg else 0,
        current_span=len(tail),
        best=best_value,
        best_period=best_period,
        best_observed=best_observed,
        pct_of_peak=pct,
        percentile=rank,
        windows=len(windows),
    )


def compute_distribution(
    series: dict[date, float],
    metric: HistoryMetric,
    total_days: int,
) -> Distribution:
    values = list(series.values())
    return Distribution(
        metric=metric.column,
        label=metric.label,
        unit=metric.unit,
        n_days=len(values),
        days_missing=max(0, total_days - len(values)),
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
# Workouts
# ---------------------------------------------------------------------------

ZONE_KEYS = ('z1', 'z2', 'z3', 'z4', 'z5')


@dataclass(frozen=True)
class WorkoutSession:
    day: date
    activity: str
    minutes: float
    zones: dict[str, float]


@dataclass(frozen=True)
class ModalityShare:
    activity: str
    sessions: int
    minutes: float
    share_pct: float
    first: date
    last: date


def parse_workouts(
    workout_rows: Sequence[dict[str, Any]],
    days: Sequence[date],
) -> list[WorkoutSession]:
    if not days:
        return []
    first, last = days[0], days[-1]
    out: list[WorkoutSession] = []
    for row in workout_rows:
        raw = row.get('date')
        if not raw:
            continue
        day = date.fromisoformat(str(raw))
        if day < first or day > last:
            continue
        zones = {}
        for key in ZONE_KEYS:
            value = to_float(row.get(f'hr_zone_{key}_min'))
            if value is not None:
                zones[key] = value
        out.append(WorkoutSession(
            day=day,
            activity=str(row.get('activity_type') or 'Unknown'),
            minutes=to_float(row.get('duration_min')) or 0.0,
            zones=zones,
        ))
    out.sort(key=lambda s: s.day)
    return out


def modality_breakdown(
    sessions: Sequence[WorkoutSession],
    start: date | None = None,
    end: date | None = None,
) -> list[ModalityShare]:
    """Share of workout minutes by activity type.

    Ranked by minutes, not session count: forty minutes of cooldown walking and
    forty minutes of interval work are one session each and are not the same
    training. A load figure that cannot see this difference will call a
    rehabilitation protocol a return to form.
    """
    picked = [s for s in sessions
              if (start is None or s.day >= start) and (end is None or s.day <= end)]
    if not picked:
        return []
    total = sum(s.minutes for s in picked)
    grouped: dict[str, list[WorkoutSession]] = defaultdict(list)
    for s in picked:
        grouped[s.activity].append(s)
    out = [
        ModalityShare(
            activity=activity,
            sessions=len(group),
            minutes=sum(s.minutes for s in group),
            share_pct=100.0 * sum(s.minutes for s in group) / total if total > EPS else 0.0,
            first=min(s.day for s in group),
            last=max(s.day for s in group),
        )
        for activity, group in grouped.items()
    ]
    out.sort(key=lambda m: -m.minutes)
    return out


def zone_totals(
    sessions: Sequence[WorkoutSession],
    start: date | None = None,
    end: date | None = None,
) -> dict[str, float]:
    totals = dict.fromkeys(ZONE_KEYS, 0.0)
    for s in sessions:
        if (start is not None and s.day < start) or (end is not None and s.day > end):
            continue
        for key, value in s.zones.items():
            totals[key] += value
    return totals


def workout_blackouts(
    sessions: Sequence[WorkoutSession],
    days: Sequence[date],
    min_days: int = BLACKOUT_MIN_DAYS,
) -> list[tuple[date, date, int]]:
    """Runs of consecutive days with no workout of any type, longest first."""
    active = {s.day for s in sessions}
    out: list[tuple[date, date, int]] = []
    run_start: date | None = None
    previous: date | None = None
    for d in days:
        if d not in active:
            if run_start is None:
                run_start = d
            previous = d
            continue
        if run_start is not None and previous is not None:
            length = (previous - run_start).days + 1
            if length >= min_days:
                out.append((run_start, previous, length))
        run_start, previous = None, None
    if run_start is not None and previous is not None:
        length = (previous - run_start).days + 1
        if length >= min_days:
            out.append((run_start, previous, length))
    out.sort(key=lambda x: -x[2])
    return out


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


def episodes_by_severity(episodes: Sequence[StrainEpisode]) -> list[StrainEpisode]:
    """Most consequential first.

    Date order buries the worst physiological event of two years among
    single-day blips, wherever it happens to fall in the calendar.
    """
    return sorted(episodes, key=lambda e: (-e.flagged_days, -e.peak_signals, e.start))


# ---------------------------------------------------------------------------
# Inferred events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InferredEvent:
    when: date
    headline: str
    evidence: list[str]


def detect_events(
    sessions: Sequence[WorkoutSession],
    days: Sequence[date],
    episodes: Sequence[StrainEpisode] = (),
) -> list[InferredEvent]:
    """Structural discontinuities in the training record.

    These are shape changes, not diagnoses. A modality that stops dead, a
    multi-week blackout, and a modality that appears from nothing and then runs
    almost daily are each individually unremarkable; arriving together within a
    few weeks they mark a discontinuity worth asking the person about. This
    function reports the shapes and their dates and stops there. Naming a cause
    is the most tempting and most harmful inference available in this data: the
    difference between "stopped bothering" and "had surgery" is invisible to
    every column in the export, and only one of those is a thing to say to
    someone.
    """
    if not days or not sessions:
        return []
    first, last = days[0], days[-1]
    events: list[InferredEvent] = []

    by_activity: dict[str, list[WorkoutSession]] = defaultdict(list)
    for s in sessions:
        by_activity[s.activity].append(s)

    for activity, group in sorted(by_activity.items()):
        first_day = min(s.day for s in group)
        last_day = max(s.day for s in group)

        if len(group) >= MODALITY_MIN_SESSIONS and (last - last_day).days >= MODALITY_QUIET_DAYS:
            recent = sum(1 for s in group if last_day - timedelta(days=90) <= s.day <= last_day)
            events.append(InferredEvent(
                when=last_day,
                headline=f'"{activity}" stops on {last_day.isoformat()} and does not resume',
                evidence=[
                    f'{len(group)} sessions in the record, {recent} of them in the 90 days to '
                    f'{last_day.isoformat()}, none in the {(last - last_day).days} days since',
                ],
            ))

        if (len(group) >= NEW_MODALITY_MIN_SESSIONS
                and (first_day - first).days >= NEW_MODALITY_MIN_OFFSET):
            early = [s for s in group if s.day <= first_day + timedelta(days=90)]
            minutes = [s.minutes for s in early if s.minutes > 0]
            detail = (f'{len(early)} sessions in its first 90 days, {len(group)} in total, '
                      f'median {statistics.median(minutes):.0f} min'
                      if minutes else f'{len(group)} sessions in total')
            events.append(InferredEvent(
                when=first_day,
                headline=f'"{activity}" appears on {first_day.isoformat()} with no prior '
                         f'occurrence in the preceding {(first_day - first).days} days',
                evidence=[detail],
            ))

    for start, end, length in workout_blackouts(sessions, days)[:2]:
        events.append(InferredEvent(
            when=start,
            headline=f'{length} consecutive days with no workout of any type '
                     f'({start.isoformat()}..{end.isoformat()})',
            evidence=[],
        ))

    if episodes:
        worst = episodes_by_severity(episodes)[0]
        others = [e.flagged_days for e in episodes if e is not worst]
        ratio = worst.flagged_days / statistics.mean(others) if others else None
        events.append(InferredEvent(
            when=worst.start,
            headline=f'Largest physiological strain episode of the record: '
                     f'{worst.flagged_days} flagged days in {worst.span_days} '
                     f'({date_span_label(worst.start, worst.end)})',
            evidence=[
                f'{ratio:.1f}x the mean flagged-day count of the other {len(others)} episodes'
                if ratio else '',
                'signals: ' + ', '.join(worst.signals),
            ],
        ))

    events.sort(key=lambda e: e.when)
    return events


# ---------------------------------------------------------------------------
# Load bands over time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Era:
    start: date
    end: date
    days: int
    band: str
    load_mean: float | None
    context: dict[str, float | None]
    # How far the smoothed load sat from the band edge at the opening boundary.
    # A small margin means the boundary is an artifact of where the edge was put.
    boundary_margin: float | None
    modalities: list[ModalityShare] = field(default_factory=list)


def band_for(load: float) -> str:
    for upper, name in ERA_BANDS:
        if load < upper:
            return name
    return ERA_BANDS[-1][1]


def band_edge_distance(load: float) -> float:
    """Distance from the nearest band edge, in minutes/day."""
    edges = [upper for upper, _name in ERA_BANDS if math.isfinite(upper)]
    return min(abs(load - edge) for edge in edges)


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


def count_band_crossings(smoothed: Sequence[float | None]) -> int:
    """How many times the smoothed series changes band at all.

    Printed beside the table because it is the honest denominator: the rows
    shown are the crossings that happened to last, not the number of times this
    person's training changed.
    """
    bands = [band_for(v) for v in smoothed if v is not None]
    return sum(1 for a, b in zip(bands, bands[1:], strict=False) if a != b)


def segment_eras(
    load_series: dict[date, float],
    days: Sequence[date],
    context_series: dict[str, dict[date, float]],
    min_era_days: int = MIN_ERA_DAYS,
    sessions: Sequence[WorkoutSession] = (),
) -> list[Era]:
    """Bucket the timeline by load band. This is not change-point detection.

    Smooth the load with a centred 28-day mean, drop each day into a fixed band,
    merge neighbouring days that share a band, absorb any stretch shorter than
    three weeks into its longer neighbour. Every boundary is therefore a
    threshold crossing at an edge chosen a priori, and which crossings survive is
    set by min_era_days rather than by anything the person did. Each row carries
    the distance from the band edge at its opening boundary so a reader can see
    how arbitrary that boundary is — some sit well under a minute/day from the
    edge, meaning a threshold moved by a rounding error would delete them.
    """
    if not days:
        return []
    smoothed = centred_means(load_series, days)
    bands: list[str | None] = [band_for(v) if v is not None else None for v in smoothed]
    if all(b is None for b in bands):
        return []

    # Stretches with no load data at all inherit the nearest classified day
    # rather than becoming a band of their own.
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
    # neighbours sharing a band, and "two adjacent identical rows" is not a
    # thing this table should ever show.
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
    for position, (start_i, end_i, _band) in enumerate(segments):
        block = days[start_i:end_i + 1]
        loads = [load_series[d] for d in block if d in load_series]
        load_mean = statistics.mean(loads) if loads else None
        context: dict[str, float | None] = {}
        for column, series in context_series.items():
            vals = [series[d] for d in block if d in series]
            context[column] = statistics.mean(vals) if vals else None
        opening = smoothed[start_i]
        eras.append(Era(
            start=block[0],
            end=block[-1],
            days=len(block),
            band=band_for(load_mean) if load_mean is not None else 'unknown',
            load_mean=load_mean,
            context=context,
            boundary_margin=(band_edge_distance(opening)
                             if position > 0 and opening is not None else None),
            modalities=modality_breakdown(sessions, block[0], block[-1]),
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
    days_measured: int
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

    A day with no measurement breaks the streak. That is the conservative
    reading: an unrecorded night is not evidence of seven hours' sleep. It also
    makes these floors, so the measured-day count is printed alongside to show
    how much room there is beneath them.
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
        days_measured=sum(1 for d in days if d in series),
        total_days=len(days),
        current=current,
        longest=longest,
        longest_start=longest_start,
        longest_end=longest_end,
    )


# ---------------------------------------------------------------------------
# Year over year
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bucket:
    label: str
    days: int
    values: dict[str, float | None]
    counts: dict[str, int]


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
        counts: dict[str, int] = {}
        for column, series in series_by_metric.items():
            vals = [series[d] for d in block if d in series]
            values[column] = statistics.mean(vals) if vals else None
            counts[column] = len(vals)
        out.append(Bucket(label=label, days=len(block), values=values, counts=counts))
    return out


MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def month_by_year_grid(
    series: dict[date, float],
    days: Sequence[date],
) -> tuple[list[int], list[tuple[str, dict[int, tuple[float, int] | None]]]]:
    """Mean by calendar month, split by year rather than pooled across years.

    Pooling is what makes a monotonic decline look like a season: every month
    cell mixes a strong year with a weak one, and the months that exist in only
    the strong year come out on top for that reason alone.
    """
    years = sorted({d.year for d in days})
    grid: list[tuple[str, dict[int, tuple[float, int] | None]]] = []
    for month in range(1, 13):
        cells: dict[int, tuple[float, int] | None] = {}
        for year in years:
            vals = [series[d] for d in days
                    if d.year == year and d.month == month and d in series]
            cells[year] = (statistics.mean(vals), len(vals)) if vals else None
        if any(cells.values()):
            grid.append((MONTH_NAMES[month - 1], cells))
    return years, grid


# ---------------------------------------------------------------------------
# Progression target
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressionTarget:
    recent_weekly: float
    next_week: float
    ceiling_weekly: float | None
    weeks_to_ceiling: int | None


def progression_target(capacity: Sequence[CapacityRow]) -> ProgressionTarget | None:
    """A next-week number anchored on what was actually achieved last month.

    Anchored on recent achieved volume, never on the personal record: the record
    is what was possible under conditions that may no longer exist, and pointing
    a recovering person at it is how re-injury happens. The step is the
    conventional 10%, which keeps the acute:chronic ratio inside the commonly
    cited safe band.
    """
    load = next((c for c in capacity if c.metric == ERA_LOAD_COLUMN), None)
    if load is None or load.current is None:
        return None
    recent_weekly = load.current * 7.0
    ceiling = load.best * 7.0 if load.best is not None else None
    weeks = None
    if ceiling and recent_weekly > EPS and ceiling > recent_weekly:
        weeks = math.ceil(math.log(ceiling / recent_weekly) / math.log(PROGRESSION_STEP))
    return ProgressionTarget(
        recent_weekly=recent_weekly,
        next_week=recent_weekly * PROGRESSION_STEP,
        ceiling_weekly=ceiling,
        weeks_to_ceiling=weeks,
    )


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
    band_crossings: int = 0
    streaks: list[Streak] = field(default_factory=list)
    episodes: list[StrainEpisode] = field(default_factory=list)
    by_year: list[Bucket] = field(default_factory=list)
    load_years: list[int] = field(default_factory=list)
    load_month_grid: list[tuple[str, dict[int, tuple[float, int] | None]]] = field(default_factory=list)
    calendar_years: int = 0
    sessions: list[WorkoutSession] = field(default_factory=list)
    modalities: list[ModalityShare] = field(default_factory=list)
    recent_modalities: list[ModalityShare] = field(default_factory=list)
    zones: dict[str, float] = field(default_factory=dict)
    recent_zones: dict[str, float] = field(default_factory=dict)
    workouts_with_zones: int = 0
    max_hr: float | None = None
    events: list[InferredEvent] = field(default_factory=list)
    target: ProgressionTarget | None = None
    suspect_days: list[tuple[str, date]] = field(default_factory=list)


def build_history(
    daily_rows: Sequence[dict[str, Any]],
    insight_rows: Sequence[dict[str, Any]],
    start: date | None = None,
    workout_rows: Sequence[dict[str, Any]] = (),
    max_hr: float | None = None,
    suspect_days: Sequence[tuple[str, date]] = (),
) -> HistoryResult:
    rows_by_date: dict[date, dict[str, Any]] = {}
    for row in daily_rows:
        if not row.get('date'):
            continue
        d = date.fromisoformat(row['date'])
        if start and d < start:
            continue
        rows_by_date[d] = row

    result = HistoryResult(suspect_days=list(suspect_days))
    if not rows_by_date:
        return result

    first, last = min(rows_by_date), max(rows_by_date)
    # A dense calendar, not just the days that produced rows: a gap in the
    # export is a gap in the person's history and windows must feel it.
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]

    result.first_day, result.last_day = first, last
    result.n_days = len(days)
    result.days_with_rows = len(rows_by_date)
    result.max_hr = max_hr

    all_metrics = list(KEY_METRICS) + [m for m in ERA_CONTEXT_METRICS if m.column not in METRICS_BY_COLUMN]
    series_by_column = {
        m.column: build_metric_series(rows_by_date, m, days, suspect_days) for m in all_metrics
    }

    for metric in KEY_METRICS:
        series = series_by_column[metric.column]
        if not series:
            continue
        result.capacity.append(compute_capacity(series, days, metric))
        records = compute_records(series, days, metric)
        if records:
            result.records[metric.column] = records
        result.distributions.append(compute_distribution(series, metric, len(days)))

    result.sessions = parse_workouts(workout_rows, days)
    result.modalities = modality_breakdown(result.sessions)
    recent_from = days[-CAPACITY_WINDOW] if len(days) >= CAPACITY_WINDOW else days[0]
    result.recent_modalities = modality_breakdown(result.sessions, recent_from, last)
    result.zones = zone_totals(result.sessions)
    result.recent_zones = zone_totals(result.sessions, recent_from, last)
    result.workouts_with_zones = sum(1 for s in result.sessions if s.zones)

    load_series = series_by_column.get(ERA_LOAD_COLUMN, {})
    result.eras = segment_eras(
        load_series,
        days,
        {m.column: series_by_column[m.column] for m in ERA_CONTEXT_METRICS},
        sessions=result.sessions,
    )
    result.band_crossings = count_band_crossings(centred_means(load_series, days))

    for rule in STREAK_RULES:
        series = series_by_column.get(rule.column)
        if series:
            result.streaks.append(compute_streak(series, days, rule))

    result.episodes = group_strain_episodes(insight_rows)
    result.events = detect_events(result.sessions, days, result.episodes)
    result.target = progression_target(result.capacity)

    yearly = {m.column: series_by_column[m.column] for m in KEY_METRICS}
    result.by_year = bucket_means(yearly, days, lambda d: str(d.year))
    result.load_years, result.load_month_grid = month_by_year_grid(load_series, days)
    result.calendar_years = len({d.year for d in days})
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(value: float | None) -> str:
    return 'n/a' if value is None else f'{value:.0f}%'


def render_situation(history: HistoryResult) -> list[str]:
    """One paragraph naming the largest changes in the record, with dates."""
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
        f'(mean of measured days in {load.current_period}) against a proven best of '
        f'**{fmt(load.best, 1)} min/day** across the 28 days {load.best_period} — the '
        f'{load.percentile:.0f}th percentile of this person\'s own 28-day windows.',
    ]

    if history.recent_modalities:
        top = history.recent_modalities[0]
        parts.append(f'Recent workout minutes are {top.share_pct:.0f}% "{top.activity}", so how '
                     f'much and what kind are separate questions here.')

    if history.episodes:
        worst = episodes_by_severity(history.episodes)[0]
        parts.append(f'The largest strain episode of the record ran '
                     f'{date_span_label(worst.start, worst.end)} '
                     f'({worst.flagged_days} flagged days of {worst.span_days}).')

    parts.append('The training record contains structural discontinuities — see **Inferred '
                 'events** immediately below, and ask what happened around those dates before '
                 'interpreting anything else in this file.')

    return ['## Situation', '', ' '.join(p for p in parts if p), '']


def render_events(history: HistoryResult) -> list[str]:
    if not history.events:
        return []
    lines = [
        '## Inferred events',
        '',
        'Structural discontinuities in the record, with their dates. These are **shapes in the '
        'data, not diagnoses**. Deviations like these are produced by injury, surgery, illness, a '
        'change of job or city, caring responsibilities, a lost device, or a change of app — and '
        'nothing in an Apple Health export distinguishes between them. They are surfaced so the '
        'pattern is not silently ignored; **ask the person what happened before assuming any '
        'cause**, and do not read reduced training as reduced motivation.',
        '',
    ]
    for event in history.events:
        lines.append(f'- **{event.when.isoformat()}** — {event.headline}')
        for item in event.evidence:
            if item:
                lines.append(f'  - {item}')
    lines.append('')
    lines.append('A cluster of these — an established modality stopping, a long blackout, a large '
                 'strain episode, then an unfamiliar low-intensity modality running almost daily '
                 '— is equally consistent with a medical event and a rehabilitation protocol, '
                 'with a gym or season ending, with a period of illness, or with a major schedule '
                 'change. This data ranks none of them above the others. If the person tells you '
                 'which it was, believe them over the numbers.')
    lines.append('')
    return lines


def render_capacity(history: HistoryResult) -> list[str]:
    if not history.capacity:
        return []
    row0 = history.capacity[0]
    lines = [
        '## Capacity gap — current vs proven best',
        '',
        f'`current` is the mean over measured days in {row0.current_period}; `best` is the best '
        '28-day mean anywhere in the record. Both are 28-day means, so they are like-for-like. '
        '**Read `pctile` first** — it places the current window among all of this person\'s '
        '28-day windows on the metric\'s own scale. `% of peak` appears only for metrics where '
        'zero is attainable and meaningful; for resting HR or HRV a ratio is trapped near 100% by '
        'construction and would read as reassuring at the bottom of the range. `n` is measured '
        'days out of 28: a low `n` means the mean rests on few days.',
        '',
        '| metric | current | n | pctile | best 28d | when | % of peak |',
        '|---|---|---|---|---|---|---|',
    ]
    for row in history.capacity:
        direction = '' if row.higher_is_better else ' (lower better)'
        pct = _pct(row.pct_of_peak) if row.zero_floored else '-'
        lines.append(
            f'| {row.label} ({row.unit}){direction} | {fmt(row.current, row.round_to)} | '
            f'{row.current_observed}/{row.current_span} | {_pct(row.percentile)} | '
            f'{fmt(row.best, row.round_to)} | {row.best_period or "n/a"} | {pct} |'
        )
    lines.append('')
    windows = max((r.windows for r in history.capacity), default=0)
    if windows:
        independent = max(1, round(windows / CAPACITY_WINDOW))
        lines.append(f'Percentiles run over {windows} rolling 28-day windows, which **overlap '
                     f'heavily**: consecutive windows share 27 of their 28 days, so the effective '
                     f'number of independent windows is roughly {independent}. Read a percentile '
                     'as "about where this sits", never as a significance test.')
        lines.append('')
    lines.append('A 28-day mean of minutes cannot see what those minutes were. Check the modality '
                 'and zone tables before treating any figure here as fitness.')
    lines.append('')
    return lines


def render_target(history: HistoryResult) -> list[str]:
    target = history.target
    if target is None:
        return []
    lines = [
        '## Working target for the coming week',
        '',
        f'- Achieved recently: **{target.recent_weekly:.0f} min/week** of exercise '
        '(last 28 days, scaled to a week)',
        f'- Conservative next step: **{target.next_week:.0f} min/week** '
        f'(+{(PROGRESSION_STEP - 1) * 100:.0f}%)',
    ]
    if target.ceiling_weekly:
        lines.append(f'- Previously sustained: {target.ceiling_weekly:.0f} min/week')
    if target.weeks_to_ceiling:
        lines.append(f'- At this step, roughly {target.weeks_to_ceiling} weeks to return to that '
                     'level, if returning to it is even the right goal')
    lines.extend([
        '',
        'Anchored on what was actually achieved last month, deliberately not on the personal '
        'record: the record was set under conditions that may no longer hold, and aiming a '
        'recovering person at it is how re-injury happens. A step much above '
        f'{PROGRESSION_MAX_RATIO:g}x the 28-day mean pushes the acute:chronic ratio out of the '
        'commonly cited safe band. This number is blind to modality and to any medical '
        'restriction — if the Inferred events section shows a discontinuity it may be wrong in '
        'either direction, and a clinician who knows the person outranks it.',
        '',
    ])
    return lines


def render_workouts(history: HistoryResult) -> list[str]:
    if not history.sessions:
        return []
    total_minutes = sum(s.minutes for s in history.sessions)
    lines = [
        '## Training modality and intensity',
        '',
        f'{len(history.sessions)} workouts in window, {total_minutes:,.0f} minutes total, '
        f'{history.workouts_with_zones} carrying heart-rate zone data.',
    ]
    if history.max_hr:
        lines.append(f'Max heart rate used for zones: **{history.max_hr:.0f} bpm** (highest '
                     'observed in the export; isolated artifacts rejected). Zones are fractions '
                     'of that: z1 <60%, z2 60-70%, z3 70-80%, z4 80-90%, z5 >=90%.')
    lines.append('')

    lines.extend(['| modality | sessions | minutes | share | first | last |',
                  '|---|---|---|---|---|---|'])
    for m in history.modalities[:10]:
        lines.append(f'| {m.activity} | {m.sessions} | {m.minutes:,.0f} | {m.share_pct:.0f}% | '
                     f'{m.first.isoformat()} | {m.last.isoformat()} |')
    lines.append('')

    if history.recent_modalities:
        lines.append('Last 28 days by minutes: ' + ', '.join(
            f'{m.activity} {m.minutes:,.0f} min ({m.share_pct:.0f}%)'
            for m in history.recent_modalities[:5]) + '.')
        lines.append('')

    if any(history.zones.values()):
        lines.extend(['| zone | all-time min | last 28d min |', '|---|---|---|'])
        for key in ZONE_KEYS:
            lines.append(f'| {key} | {history.zones.get(key, 0.0):,.0f} | '
                         f'{history.recent_zones.get(key, 0.0):,.0f} |')
        lines.append('')
    lines.append('Zone minutes come only from recorded workouts, so they undercount incidental '
                 'activity. A modality label is whatever the user or app chose; a generic label '
                 'like "Cooldown" or "Other" carrying a large share of minutes is something to '
                 'ask about, not something to score as training.')
    lines.append('')
    return lines


def render_eras(history: HistoryResult) -> list[str]:
    if not history.eras:
        return []
    lines = [
        '## Load bands over time',
        '',
        f'The exercise series is smoothed with a centred 28-day mean and bucketed into fixed '
        f'bands ({ERA_BAND_LEGEND}); adjacent days sharing a band merge, and stretches under '
        f'{MIN_ERA_DAYS} days are absorbed into their longer neighbour. **This is bucketing, not '
        f'change detection.** The smoothed series crosses a band edge {history.band_crossings} '
        f'times in this record; the {len(history.eras)} rows below are the crossings that '
        f'happened to last, which is a property of the {MIN_ERA_DAYS}-day rule and of where the '
        'edges were drawn, not of anything the person decided. `margin` is how far the smoothed '
        'load sat from the band edge at that row\'s opening boundary — under ~1 min/day means the '
        'boundary would move or vanish under a trivially different threshold. Context columns are '
        'means over the same stretch, not causes.',
        '',
        '| period | days | band | exercise min/day | margin | top modality | sleep h | HRV ms | '
        'resting HR | weight kg |',
        '|---|---|---|---|---|---|---|---|---|---|',
    ]
    for era in history.eras:
        ctx = era.context
        top = ''
        if era.modalities:
            m = era.modalities[0]
            top = f'{m.activity} {m.share_pct:.0f}%'
        margin = f'{era.boundary_margin:.1f}' if era.boundary_margin is not None else '-'
        lines.append(
            f'| {era.start.isoformat()}..{era.end.isoformat()} | {era.days} | {era.band} | '
            f'{fmt(era.load_mean, 1)} | {margin} | {top or "-"} | '
            f'{fmt(ctx.get("sleep_asleep_hours"), 2)} | '
            f'{fmt(ctx.get("hrv_sdnn"), 1)} | {fmt(ctx.get("resting_hr"), 1)} | '
            f'{fmt(ctx.get("body_mass_kg"), 1)} |'
        )
    lines.append('')
    lines.append('A band counts minutes, not effort: a stretch labelled "active" can be almost '
                 'entirely low-intensity or rehabilitation work. Read the top-modality column '
                 'before reading the band.')
    lines.append('')
    return lines


def render_distributions(history: HistoryResult) -> list[str]:
    if not history.distributions:
        return []
    lines = [
        '## Distribution of daily values (whole record)',
        '',
        'Use this to read any single number in this file. Percentiles are over **measured days '
        'only** — nothing is imputed, so missing days are absent from these statistics rather '
        'than entered as zero, and a 0 here means a day that was measured at zero. Where '
        '`missing` is large the percentiles describe the days the device was recording, which '
        'need not be a fair sample of all days.',
        '',
        '| metric | measured days | missing | p10 | p25 | median | p75 | p90 | min | max |',
        '|---|---|---|---|---|---|---|---|---|---|',
    ]
    for dist in history.distributions:
        r = dist.round_to
        lines.append(
            f'| {dist.label} ({dist.unit}) | {dist.n_days} | {dist.days_missing} | '
            f'{fmt(dist.p10, r)} | {fmt(dist.p25, r)} | {fmt(dist.p50, r)} | {fmt(dist.p75, r)} | '
            f'{fmt(dist.p90, r)} | {fmt(dist.lowest, r)} | {fmt(dist.highest, r)} |'
        )
    lines.append('')
    worst = max(history.distributions, key=lambda d: d.days_missing, default=None)
    if worst is not None and worst.days_missing > 0.1 * max(1, worst.n_days + worst.days_missing):
        lines.append(f'`{worst.metric}` is the least complete series here ({worst.days_missing} '
                     'days missing) and its missingness is unlikely to be random — outdoor and '
                     'daylight metrics drop out in winter, which is exactly when their true '
                     'values are lowest. Do not compare its means across seasons.')
        lines.append('')
    return lines


def render_streaks(history: HistoryResult) -> list[str]:
    if not history.streaks:
        return []
    lines = [
        '## Streaks and consistency',
        '',
        '| threshold | days met | measured days | of days | current | longest | longest ran |',
        '|---|---|---|---|---|---|---|',
    ]
    for s in history.streaks:
        span = (f'{s.longest_start.isoformat()}..{s.longest_end.isoformat()}'
                if s.longest_start and s.longest_end else 'n/a')
        lines.append(
            f'| {s.label} | {s.days_met} | {s.days_measured} | {s.total_days} | {s.current} | '
            f'{s.longest} | {span} |'
        )
    lines.append('')
    lines.append('A day with no measurement breaks a streak, so these are floors.')
    lines.append('')
    return lines


def render_episodes(history: HistoryResult, limit: int = 10) -> list[str]:
    if not history.episodes:
        return []
    ranked = episodes_by_severity(history.episodes)[:limit]
    lines = [
        '## Physiological strain episodes (most severe first)',
        '',
        f'{len(history.episodes)} episode(s) across the record; flagged days within '
        f'{EPISODE_MAX_GAP_DAYS} clear days of each other are one episode. A flagged day is one '
        'where two or more of wrist temperature, resting HR, HRV, respiratory rate and SpO2 moved '
        'the wrong way against the personal 60-day baseline together. Sorted by flagged days '
        'rather than by date, because one of these is usually far more consequential than the '
        'rest and date order hides it.',
        '',
        '| episode | span days | flagged days | peak signals | signals |',
        '|---|---|---|---|---|',
    ]
    for e in ranked:
        lines.append(f'| {date_span_label(e.start, e.end)} | {e.span_days} | {e.flagged_days} | '
                     f'{e.peak_signals} | {", ".join(e.signals)} |')
    if len(ranked) < len(history.episodes):
        lines.append('')
        lines.append(f'(top {len(ranked)} of {len(history.episodes)} by severity)')
    lines.append('')
    return lines


SCOPE_LABELS = {'day': 'best day', '7d': 'best 7 days', '28d': 'best 28 days'}


def render_records(history: HistoryResult) -> list[str]:
    if not history.records:
        return []
    lines = [
        '## Personal records (with dates)',
        '',
        'Per-day means over measured days, so scopes compare directly; `total` sums only the '
        'measured days and undercounts a window with gaps. `n` is measured days in the window. '
        'For resting HR the "record" is the lowest.',
        '',
        '| metric | scope | per day | total | n | period |',
        '|---|---|---|---|---|---|',
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
                f'{fmt(entry.per_day, metric.round_to)} | {total} | '
                f'{entry.observations}/{entry.span} | {entry.period}{tie} |'
            )
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
    lines.append('Means over measured days. Partial years at either end are not annualised; the '
                 '`days` column says how much of each year is present.')
    lines.append('')

    if history.load_month_grid and len(history.load_years) > 1:
        years = history.load_years
        lines.append('Exercise min/day by month, **split by year** rather than pooled: pooling '
                     'months across years turns a monotonic decline into a fake seasonal cycle, '
                     'and a month present in only one year then reflects that year rather than '
                     'that season. Cells are `mean (measured days)`.')
        lines.append('')
        lines.append('| month | ' + ' | '.join(str(y) for y in years) + ' |')
        lines.append('|---' * (len(years) + 1) + '|')
        for label, cells in history.load_month_grid:
            row = []
            for year in years:
                cell = cells.get(year)
                row.append(f'{cell[0]:.1f} ({cell[1]})' if cell else '-')
            lines.append(f'| {label} | ' + ' | '.join(row) + ' |')
        lines.append('')
        lines.append('A weekday breakdown was computed and dropped: no weekday differed from the '
                     'rest by more than sampling noise over this record.')
        lines.append('')
    return lines


def render_limits(history: HistoryResult, thin_metrics: Sequence[str] = ()) -> list[str]:
    """The section that stops a reader treating this file as more than it is."""
    lines = [
        '## What this data cannot tell you',
        '',
        '- **Nothing here is causal.** Load bands, correlations and contrasts describe what '
        'co-occurred. A fall in exercise sits next to whatever else was happening in this '
        'person\'s life, none of which is in this export.',
        '- **Absence is not zero, and is not treated as zero.** Missing days are excluded from '
        'every mean, percentile and record here, and each period prints its measured-day count. '
        'HealthKit does not distinguish "nothing happened" from "nothing was recorded", so '
        'neither does this file.',
        '- **Missingness is not random.** Outdoor metrics drop out in winter; everything drops out '
        'when the watch is off. A metric with many missing days describes the days it was '
        'recording, which may be systematically unlike the rest.',
        '- **Volume is not training.** Minutes counted by the watch include rehabilitation, '
        'walking and cooldowns. Check the modality and zone tables before treating a load figure '
        'as fitness work.',
        '- **No context on why.** Injury, surgery, illness beyond what the vitals caught, work, '
        'travel, mood, medication, life events — none of it is recorded here. Do not infer '
        'motivation, discipline or intent from these numbers. Ask.',
        '- **Records are records, not targets.** A proven peak was reached once under conditions '
        'that may no longer exist. It bounds what has been possible, not what is advisable now.',
        '- **Not clinical data.** Consumer-wearable estimates with device-specific bias; VO2 max '
        'and body composition especially. Nothing here diagnoses anything, and any inference '
        'about a medical event is a question for the person and their clinician, not a finding.',
    ]
    if history.suspect_days:
        shown = ', '.join(f'{column} on {d.isoformat()}' for column, d in history.suspect_days[:6])
        lines.append(f'- **Sensor artifacts excluded:** {len(history.suspect_days)} single-day '
                     'value(s) dropped as physiologically implausible against their immediate '
                     f'neighbours ({shown}). They are absent from every statistic here.')
    if history.calendar_years < 3:
        lines.append(f'- **Short history.** {history.n_days} day(s) across '
                     f'{history.calendar_years} calendar year(s): seasonal claims are unsupported.')
    if thin_metrics:
        lines.append('- **Thinly measured here:** ' + ', '.join(thin_metrics) +
                     ' — treat their percentiles and records as indicative only.')
    lines.append('')
    return lines


def render_history_highlights(history: HistoryResult) -> list[str]:
    """Compact history block for the human-facing report."""
    lines: list[str] = []
    lines.extend(render_events(history))
    lines.extend(render_capacity(history))
    lines.extend(render_target(history))
    lines.extend(render_workouts(history))
    lines.extend(render_eras(history))
    lines.extend(render_records(history))
    lines.extend(render_streaks(history))
    return lines
