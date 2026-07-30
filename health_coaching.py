"""Parse Apple Health exports and build coaching-ready daily/workout/weekly outputs.

High-volume record types are folded into daily buckets during the XML scan and
never retained; only samples needed for workout time-window joins, sleep staging,
and the legacy flat CSV stay in memory.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from collections.abc import Iterable

from health_metrics import (
    ALL_PARSED_TYPES,
    COLUMN_META,
    DAILY_SPECS,
    EFFORT_TYPE,
    MINDFUL_TYPE,
    SLEEP_TYPE,
    SPARSE_BY_DESIGN,
    SPECS_BY_TYPE,
    STAND_HOUR_TYPE,
    WEAR_SIGNAL_TYPES,
    WINDOW_JOIN_TYPES,
    daily_column_order,
)

# Core metrics kept in the legacy full_health_data.csv (unchanged for compatibility)
TARGET_TYPES = {
    'HKQuantityTypeIdentifierStepCount',
    'HKQuantityTypeIdentifierActiveEnergyBurned',
    'HKQuantityTypeIdentifierHeartRate',
    'HKQuantityTypeIdentifierRestingHeartRate',
    'HKQuantityTypeIdentifierHeartRateVariabilitySDNN',
    'HKCategoryTypeIdentifierSleepAnalysis',
    'HKQuantityTypeIdentifierBodyMass',
    'HKQuantityTypeIdentifierBodyFatPercentage',
    'HKQuantityTypeIdentifierVO2Max',
    'HKQuantityTypeIdentifierRespiratoryRate',
    'HKCategoryTypeIdentifierAppleStandHour',
}

COACHING_RECORD_TYPES = ALL_PARSED_TYPES

WORKOUT_ENRICHMENT_TYPES = {
    'HKQuantityTypeIdentifierHeartRate',
    'HKQuantityTypeIdentifierCyclingPower',
    'HKQuantityTypeIdentifierCyclingCadence',
}

# Records retained individually rather than folded into daily buckets: those
# needed for workout time-window joins, sleep staging, the legacy flat CSV, and
# the multi-device de-duplication passes (defined further down).
RETAINED_TYPES = WINDOW_JOIN_TYPES | {SLEEP_TYPE} | TARGET_TYPES | {
    'HKQuantityTypeIdentifierDistanceWalkingRunning',
    'HKQuantityTypeIdentifierFlightsClimbed',
    'HKQuantityTypeIdentifierDietaryEnergyConsumed',
    'HKQuantityTypeIdentifierDietaryProtein',
    'HKQuantityTypeIdentifierDietaryCarbohydrates',
    'HKQuantityTypeIdentifierDietaryFatTotal',
    'HKQuantityTypeIdentifierDietaryFiber',
    'HKQuantityTypeIdentifierDietarySugar',
    'HKQuantityTypeIdentifierDietarySodium',
}

SLEEP_IN_BED = 'HKCategoryValueSleepAnalysisInBed'
SLEEP_ASLEEP = 'HKCategoryValueSleepAnalysisAsleep'
SLEEP_ASLEEP_UNSPEC = 'HKCategoryValueSleepAnalysisAsleepUnspecified'
SLEEP_REM = 'HKCategoryValueSleepAnalysisAsleepREM'
SLEEP_CORE = 'HKCategoryValueSleepAnalysisAsleepCore'
SLEEP_DEEP = 'HKCategoryValueSleepAnalysisAsleepDeep'
SLEEP_AWAKE = 'HKCategoryValueSleepAnalysisAwake'

ASLEEP_STAGES = {SLEEP_ASLEEP, SLEEP_ASLEEP_UNSPEC, SLEEP_REM, SLEEP_CORE, SLEEP_DEEP}
STAGED_VALUES = {SLEEP_REM, SLEEP_CORE, SLEEP_DEEP}

STRENGTH_WORKOUT_TYPES = {
    'HKWorkoutActivityTypeTraditionalStrengthTraining',
    'HKWorkoutActivityTypeFunctionalStrengthTraining',
    'HKWorkoutActivityTypeCoreTraining',
}

CYCLING_WORKOUT_TYPES = {'HKWorkoutActivityTypeCycling'}

WALKING_WORKOUT_TYPES = {
    'HKWorkoutActivityTypeWalking',
    'HKWorkoutActivityTypeHiking',
}

DEFAULT_MAX_HR = 190.0

HR_ZONE_BOUNDS = (
    (0.60, 'z1'),
    (0.70, 'z2'),
    (0.80, 'z3'),
    (0.90, 'z4'),
    (1.01, 'z5'),
)

MODERATE_METS = 3.0
VIGOROUS_METS = 6.0

# Wear-time classification thresholds (distinct hours with a watch sample).
WEAR_FULL_HOURS = 18
WEAR_PARTIAL_HOURS = 8


@dataclass(slots=True)
class HealthRecord:
    type: str
    value: float | None
    category_value: str
    unit: str
    source_name: str
    start: datetime
    end: datetime
    creation: str
    start_raw: str
    end_raw: str


@dataclass
class Workout:
    workout_activity_type: str
    start: datetime
    end: datetime
    start_raw: str
    end_raw: str
    creation: str
    duration: float | None
    duration_unit: str
    total_energy: float | None
    total_energy_unit: str
    total_distance: float | None
    total_distance_unit: str
    source_name: str
    statistics: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


def normalize_source(name: str) -> str:
    """Apple writes the same device with different spacing across OS versions.

    "Benas's Apple\\xa0Watch" (non-breaking space), "Benas's Apple Watch" and
    "Apple Watch" are all one device; without folding them together, per-source
    logic like sleep-source selection sees phantom extra devices.
    """
    return re.sub(r'\s+', ' ', (name or '').replace('\xa0', ' ')).strip()


def is_watch_source(name: str) -> bool:
    return 'watch' in normalize_source(name).lower()


class DailyAccumulator:
    """Folds samples into per-day aggregates without retaining the samples."""

    def __init__(self) -> None:
        self._sum: dict[tuple[date, str], float] = defaultdict(float)
        self._count: dict[tuple[date, str], int] = defaultdict(int)
        self._min: dict[tuple[date, str], float] = {}
        self._max: dict[tuple[date, str], float] = {}
        self._latest: dict[tuple[date, str], tuple[datetime, float]] = {}
        self.days: set[date] = set()

    def add(self, day: date, column: str, agg: str, value: float,
            when: datetime | None = None) -> None:
        self.days.add(day)
        key = (day, column)
        if agg in ('sum', 'mean'):
            # Both need the running total and the count; which one is read back
            # is decided in get().
            self._sum[key] += value
            self._count[key] += 1
        elif agg == 'min':
            cur = self._min.get(key)
            if cur is None or value < cur:
                self._min[key] = value
        elif agg == 'max':
            cur = self._max.get(key)
            if cur is None or value > cur:
                self._max[key] = value
        elif agg == 'latest':
            prev = self._latest.get(key)
            stamp = when or datetime.min
            if prev is None or stamp > prev[0]:
                self._latest[key] = (stamp, value)

    def get(self, day: date, column: str, agg: str) -> float | None:
        key = (day, column)
        # .get() rather than [] throughout: _sum is a defaultdict, so indexing a
        # missing key would silently create it.
        if agg == 'sum':
            return self._sum.get(key)
        if agg == 'mean':
            n = self._count.get(key, 0)
            return self._sum.get(key, 0.0) / n if n else None
        if agg == 'min':
            return self._min.get(key)
        if agg == 'max':
            return self._max.get(key)
        if agg == 'latest':
            entry = self._latest.get(key)
            return entry[1] if entry else None
        return None


class WearTracker:
    """Counts distinct clock hours per day that carry a watch-sourced sample."""

    def __init__(self) -> None:
        self.hours: dict[date, set[int]] = defaultdict(set)

    def mark(self, start: datetime, end: datetime) -> None:
        day = start.date()
        self.hours[day].add(start.hour)
        # A sample can span an hour boundary; credit the end hour too, but never
        # let a single long record (e.g. a multi-hour basal-energy roll-up)
        # paint an entire day as "worn".
        if start < end <= start + timedelta(hours=2) and end.date() == day:
            self.hours[day].add(end.hour)

    def wear_hours(self, day: date) -> int:
        return len(self.hours.get(day, ()))

    @staticmethod
    def classify(hours: int) -> str:
        if hours >= WEAR_FULL_HOURS:
            return 'full'
        if hours >= WEAR_PARTIAL_HOURS:
            return 'partial'
        if hours > 0:
            return 'minimal'
        return 'none'


class EffortTracker:
    """Time-weighted METs plus moderate/vigorous minutes from PhysicalEffort."""

    def __init__(self) -> None:
        self.weighted: dict[date, float] = defaultdict(float)
        self.minutes: dict[date, float] = defaultdict(float)
        self.moderate: dict[date, float] = defaultdict(float)
        self.vigorous: dict[date, float] = defaultdict(float)

    def add(self, day: date, mets: float, start: datetime, end: datetime) -> None:
        span = (end - start).total_seconds() / 60.0
        # PhysicalEffort samples are short buckets; guard against zero-length
        # and against outliers that would dominate the weighted mean.
        if span <= 0:
            span = 1.0
        span = min(span, 30.0)
        self.weighted[day] += mets * span
        self.minutes[day] += span
        if mets >= VIGOROUS_METS:
            self.vigorous[day] += span
        elif mets >= MODERATE_METS:
            self.moderate[day] += span

    def average(self, day: date) -> float | None:
        mins = self.minutes.get(day, 0.0)
        return self.weighted[day] / mins if mins else None


@dataclass
class ExportData:
    type_counts: Counter = field(default_factory=Counter)
    records: list[HealthRecord] = field(default_factory=list)
    workouts: list[Workout] = field(default_factory=list)
    seen_record_keys: set[int] = field(default_factory=set)
    timezone_label: str = ''
    daily: DailyAccumulator = field(default_factory=DailyAccumulator)
    wear: WearTracker = field(default_factory=WearTracker)
    effort: EffortTracker = field(default_factory=EffortTracker)
    stand_hours: dict[date, int] = field(default_factory=lambda: defaultdict(int))
    mindful_minutes: dict[date, float] = field(default_factory=lambda: defaultdict(float))
    first_seen: dict[str, date] = field(default_factory=dict)
    sources: Counter = field(default_factory=Counter)
    metric_sources: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def mark_seen(self, *parts: str) -> bool:
        """True if this record was already ingested.

        Stores a hash rather than the tuple itself: on a million-record export
        the tuple-of-strings form costs hundreds of MB, the hash costs ~30.
        """
        key = hash('\x1f'.join(parts))
        if key in self.seen_record_keys:
            return True
        self.seen_record_keys.add(key)
        return False


def parse_health_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        if len(raw) >= 25 and raw[-5] in '+-':
            return datetime.strptime(raw, '%Y-%m-%d %H:%M:%S %z')
        return datetime.strptime(raw[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def local_calendar_date(dt: datetime) -> date:
    """Calendar date as it read on the clock where the sample was taken.

    Apple writes wall-clock time with the offset appended, so the date part is
    already local to wherever the user was. Converting to the machine's own
    timezone (the previous behaviour) silently shifted late-evening samples into
    the next day whenever the export was processed in a different timezone than
    it was recorded in.
    """
    return dt.date()


def parse_float(value: str) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def energy_to_kcal(amount: float | None, unit: str) -> float | None:
    if amount is None:
        return None
    u = (unit or '').lower()
    if u == 'kj':
        return amount / 4.184
    return amount


def distance_to_km(amount: float | None, unit: str) -> float | None:
    if amount is None:
        return None
    u = (unit or '').lower()
    if u in ('m', 'meter', 'meters'):
        return amount / 1000.0
    if u in ('mi', 'mile', 'miles'):
        return amount * 1.60934
    return amount


def duration_to_minutes(amount: float | None, unit: str) -> float | None:
    if amount is None:
        return None
    u = (unit or 'min').lower()
    if u in ('s', 'sec', 'second', 'seconds'):
        return amount / 60.0
    if u in ('h', 'hr', 'hour', 'hours'):
        return amount * 60.0
    return amount


def friendly_workout_type(hk_type: str) -> str:
    if not hk_type:
        return ''
    name = hk_type.replace('HKWorkoutActivityType', '')
    if not name:
        return hk_type
    return re.sub(r'(?<!^)(?=[A-Z])', ' ', name)


def workout_id_for(workout: Workout) -> str:
    key = f"{workout.start_raw}|{workout.end_raw}|{workout.workout_activity_type}|{workout.source_name}"
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]


def infer_timezone_label(records: list[HealthRecord], workouts: list[Workout]) -> str:
    for item in list(records[:50]) + [None]:
        raw = item.start_raw if item else ''
        if not raw:
            continue
        parts = raw.rsplit(' ', 1)
        if len(parts) == 2 and parts[1][0] in '+-':
            return parts[1]
    for w in workouts[:20]:
        parts = w.start_raw.rsplit(' ', 1)
        if len(parts) == 2 and parts[1][0] in '+-':
            return parts[1]
    return 'unknown (naive local dates)'


# Metrics that several devices record over the same wall-clock time. An iPhone
# in a pocket and a watch on the wrist both count the same steps, so naively
# summing the export's records inflates the totals badly (~35% on step counts
# for a user who carries both). Apple Health itself de-duplicates by time
# window before showing a number; these need the same treatment.
DEDUP_INTERVAL_TYPES: dict[str, str] = {
    'HKQuantityTypeIdentifierStepCount': 'steps',
    'HKQuantityTypeIdentifierDistanceWalkingRunning': 'distance_km',
    'HKQuantityTypeIdentifierFlightsClimbed': 'flights_climbed',
    'HKQuantityTypeIdentifierActiveEnergyBurned': 'active_kcal',
}

# Metrics logged as instantaneous entries rather than intervals, where two apps
# mirroring the same meals would double the day's total. Interval logic cannot
# help here (a meal has no duration), so one source wins the whole day.
DEDUP_PRIMARY_TYPES: dict[str, str] = {
    'HKQuantityTypeIdentifierDietaryEnergyConsumed': 'diet_kcal',
    'HKQuantityTypeIdentifierDietaryProtein': 'diet_protein_g',
    'HKQuantityTypeIdentifierDietaryCarbohydrates': 'diet_carbs_g',
    'HKQuantityTypeIdentifierDietaryFatTotal': 'diet_fat_g',
    'HKQuantityTypeIdentifierDietaryFiber': 'diet_fiber_g',
    'HKQuantityTypeIdentifierDietarySugar': 'diet_sugar_g',
    'HKQuantityTypeIdentifierDietarySodium': 'diet_sodium_mg',
}


def source_priority(name: str) -> int:
    """Lower wins. The wrist beats the pocket; sync/mirror apps come last."""
    low = normalize_source(name).lower()
    if 'watch' in low:
        return 0
    if 'iphone' in low or 'phone' in low:
        return 1
    return 2


def _merge_intervals(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    spans.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _intersects(
    merged: list[tuple[datetime, datetime]],
    starts: list[datetime],
    start: datetime,
    end: datetime,
) -> bool:
    if not merged:
        return False
    import bisect

    is_point = start >= end
    i = bisect.bisect_right(starts, start) - 1
    if i >= 0:
        m_start, m_end = merged[i]
        if is_point:
            if m_start <= start < m_end:
                return True
        elif start < m_end:
            return True
    if not is_point:
        j = i + 1
        if j < len(merged) and merged[j][0] < end:
            return True
    return False


def dedup_interval_sum(
    records: list[HealthRecord],
    hk_type: str,
    convert,
) -> dict[date, float]:
    """Per-day total counting each stretch of time only once.

    Sources are taken best-first (watch, then phone, then anything else). A
    record is accepted only if its time span is not already covered by a
    higher-priority device, so a phone still contributes during hours the watch
    was off the wrist instead of being discarded wholesale.
    """
    by_day: dict[date, list[HealthRecord]] = defaultdict(list)
    for rec in records:
        if rec.type == hk_type and rec.value is not None:
            by_day[local_calendar_date(rec.start)].append(rec)

    totals: dict[date, float] = {}
    for day, recs in by_day.items():
        # Each distinct source is its own tier: a device renamed mid-history
        # ("Benas iPhone" -> "Benas's iPhone") never overlaps itself, but two
        # genuinely different apps do.
        by_source: dict[str, list[HealthRecord]] = defaultdict(list)
        for rec in recs:
            by_source[normalize_source(rec.source_name)].append(rec)

        order = sorted(by_source, key=lambda s: (source_priority(s), -len(by_source[s]), s))
        merged: list[tuple[datetime, datetime]] = []
        starts: list[datetime] = []
        total = 0.0

        for source in order:
            accepted: list[tuple[datetime, datetime]] = []
            for rec in sorted(by_source[source], key=lambda r: r.start):
                if _intersects(merged, starts, rec.start, rec.end):
                    continue
                accepted.append((rec.start, max(rec.end, rec.start)))
                total += convert(rec.value, rec.unit)
            if accepted:
                merged = _merge_intervals(merged + accepted)
                starts = [m[0] for m in merged]

        totals[day] = total
    return totals


def dedup_primary_sum(
    records: list[HealthRecord],
    hk_type: str,
    convert,
) -> dict[date, float]:
    """Per-day total from whichever single source logged the most that day."""
    by_day: dict[date, dict[str, list[HealthRecord]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        if rec.type == hk_type and rec.value is not None:
            by_day[local_calendar_date(rec.start)][normalize_source(rec.source_name)].append(rec)

    totals: dict[date, float] = {}
    for day, sources in by_day.items():
        best = max(sources, key=lambda s: (len(sources[s]), -source_priority(s)))
        totals[day] = sum(convert(r.value, r.unit) for r in sources[best])
    return totals


def is_stand_hour_met(category_value: str) -> bool:
    label = (category_value or '').lower()
    if label:
        # "applestandhour" is a substring of both the Stood and Idle category
        # values, so only "idle"/"stood" can tell them apart. Pre-iOS-13 exports
        # predate the Idle case and use the bare value; every record in that
        # format is a met hour, since idle hours weren't sampled yet.
        return 'idle' not in label
    return False


def ingest_record(
    data: ExportData,
    record_type: str,
    value_raw: str,
    unit: str,
    source: str,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Fold one parsed record into the daily aggregates and trackers."""
    day = local_calendar_date(start_dt)
    numeric = parse_float(value_raw)

    if record_type not in data.first_seen or day < data.first_seen[record_type]:
        data.first_seen[record_type] = day

    if record_type in WEAR_SIGNAL_TYPES and is_watch_source(source):
        data.wear.mark(start_dt, end_dt)

    if record_type == EFFORT_TYPE:
        if numeric is not None:
            data.effort.add(day, numeric, start_dt, end_dt)
        return

    if record_type == STAND_HOUR_TYPE:
        if is_stand_hour_met(value_raw):
            data.stand_hours[day] += 1
        return

    if record_type == MINDFUL_TYPE:
        minutes = (end_dt - start_dt).total_seconds() / 60.0
        if minutes > 0:
            data.mindful_minutes[day] += minutes
        return

    specs = SPECS_BY_TYPE.get(record_type)
    if not specs or numeric is None:
        return
    clean_source = normalize_source(source)
    for spec in specs:
        data.daily.add(day, spec.column, spec.agg, spec.convert(numeric, unit), when=end_dt)
        data.metric_sources[spec.column][clean_source] += 1


class ProgressFile:
    """Binary file wrapper that draws a progress bar as it is read.

    A multi-hundred-MB export takes a couple of minutes to stream, and the
    parser is silent the whole time, which is indistinguishable from a hang.
    Byte position is the honest progress signal here: record counts are not
    known until the file has already been read.
    """

    BAR_WIDTH = 28

    def __init__(self, path: str, label: str = '', stream=None, min_interval: float = 0.25):
        self._f = open(path, 'rb')
        self.total = os.path.getsize(path)
        self.read_bytes = 0
        self.records = 0
        self.label = label or os.path.basename(path)
        self._stream = stream or sys.stdout
        self._tty = hasattr(self._stream, 'isatty') and self._stream.isatty()
        self._min_interval = min_interval
        self._last_render = 0.0
        self._last_pct = -1
        self._start = time.monotonic()

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        self.read_bytes += len(chunk)
        self._render()
        return chunk

    def _render(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_render) < self._min_interval:
            return
        self._last_render = now

        pct = (100.0 * self.read_bytes / self.total) if self.total else 100.0
        pct = min(pct, 100.0)
        elapsed = now - self._start

        if not self._tty:
            # Piped or redirected: emit a line every 10% instead of redrawing.
            step = int(pct // 10)
            if not force and step <= self._last_pct:
                return
            self._last_pct = step
            self._stream.write(
                f"  {self.label}: {pct:5.1f}%  {self.records:,} records  {elapsed:.0f}s\n")
            self._stream.flush()
            return

        filled = int(self.BAR_WIDTH * pct / 100.0)
        bar = '#' * filled + '.' * (self.BAR_WIDTH - filled)
        eta = ''
        if pct > 1.0 and pct < 100.0:
            eta = f"  ~{elapsed * (100.0 - pct) / pct:.0f}s left"
        self._stream.write(
            f"\r  [{bar}] {pct:5.1f}%  {self.read_bytes / 1048576:,.0f}/"
            f"{self.total / 1048576:,.0f} MB  {self.records:,} records  {elapsed:.0f}s{eta}   ")
        self._stream.flush()

    def finish(self) -> None:
        self._render(force=True)
        if self._tty:
            self._stream.write('\n')
        self._stream.flush()

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> ProgressFile:
        return self

    def __exit__(self, *exc) -> None:
        self.finish()
        self.close()


def iter_export_xml(filepath: str, data: ExportData, show_progress: bool = True) -> int:
    import xml.etree.ElementTree as ET

    size_mb = os.path.getsize(filepath) / 1048576
    print(f"Processing: {filepath} ({size_mb:,.0f} MB)")
    added = 0

    progress: ProgressFile | None = None
    parse_source: Any = filepath
    if show_progress:
        progress = ProgressFile(filepath, label=os.path.basename(filepath))
        parse_source = progress

    try:
        context = ET.iterparse(parse_source, events=('end',))
        for _event, elem in context:
            if elem.tag == 'Record':
                record_type = elem.get('type') or ''
                data.type_counts[record_type] += 1
                if progress is not None:
                    progress.records += 1

                if record_type in ALL_PARSED_TYPES:
                    start_raw = elem.get('startDate', '')
                    end_raw = elem.get('endDate', '')
                    value_raw = elem.get('value', '') or ''
                    unit = elem.get('unit', '') or ''
                    source_name = elem.get('sourceName', '') or ''
                    creation = elem.get('creationDate', '') or ''

                    if not data.mark_seen(creation, start_raw, end_raw, record_type,
                                          value_raw, source_name):
                        start_dt = parse_health_datetime(start_raw)
                        end_dt = parse_health_datetime(end_raw)
                        if start_dt and end_dt:
                            data.sources[normalize_source(source_name)] += 1
                            ingest_record(data, record_type, value_raw, unit,
                                          source_name, start_dt, end_dt)
                            if record_type in RETAINED_TYPES:
                                numeric = parse_float(value_raw)
                                data.records.append(
                                    HealthRecord(
                                        type=record_type,
                                        value=numeric,
                                        category_value=value_raw if numeric is None else '',
                                        unit=unit,
                                        source_name=source_name,
                                        start=start_dt,
                                        end=end_dt,
                                        creation=creation,
                                        start_raw=start_raw,
                                        end_raw=end_raw,
                                    )
                                )
                            added += 1
                elem.clear()

            elif elem.tag == 'Workout':
                data.type_counts[f"Workout:{elem.get('workoutActivityType', '')}"] += 1

                stats: dict[str, dict[str, Any]] = {}
                metadata: dict[str, str] = {}
                for child in elem:
                    if child.tag == 'WorkoutStatistics':
                        stype = child.get('type') or ''
                        stats[stype] = {
                            'average': parse_float(child.get('average', '')),
                            'minimum': parse_float(child.get('minimum', '')),
                            'maximum': parse_float(child.get('maximum', '')),
                            'sum': parse_float(child.get('sum', '')),
                            'unit': child.get('unit', '') or '',
                        }
                    elif child.tag == 'MetadataEntry':
                        k = child.get('key') or ''
                        if k:
                            metadata[k] = child.get('value') or ''

                start_raw = elem.get('startDate', '')
                end_raw = elem.get('endDate', '')
                start_dt = parse_health_datetime(start_raw)
                end_dt = parse_health_datetime(end_raw)
                if start_dt and end_dt:
                    data.workouts.append(
                        Workout(
                            workout_activity_type=elem.get('workoutActivityType', '') or '',
                            start=start_dt,
                            end=end_dt,
                            start_raw=start_raw,
                            end_raw=end_raw,
                            creation=elem.get('creationDate', '') or '',
                            duration=parse_float(elem.get('duration', '')),
                            duration_unit=elem.get('durationUnit', 'min') or 'min',
                            total_energy=parse_float(elem.get('totalEnergyBurned', '')),
                            total_energy_unit=elem.get('totalEnergyBurnedUnit', 'Cal') or 'Cal',
                            total_distance=parse_float(elem.get('totalDistance', '')),
                            total_distance_unit=elem.get('totalDistanceUnit', '') or '',
                            source_name=elem.get('sourceName', '') or '',
                            statistics=stats,
                            metadata=metadata,
                        )
                    )
                    added += 1
                elem.clear()

            elif elem.tag == 'ActivitySummary':
                data.type_counts['ActivitySummary'] += 1
                elem.clear()
    finally:
        if progress is not None:
            progress.finish()
            progress.close()

    print(f"  Loaded {len(data.records):,} retained samples, {len(data.workouts):,} workouts, "
          f"{len(data.daily.days):,} days aggregated")
    return added


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

def _hours(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds(), 0) / 3600.0


# Longest break in recorded sleep that still counts as the same night. Brief
# awakenings are logged as Awake segments and bridge the gap, so this only needs
# to be long enough to tolerate a stretch the watch failed to record at all.
SLEEP_SESSION_GAP_MINUTES = 90.0


def _cluster_sleep_sessions(
    segments: list[tuple[datetime, datetime, str]],
) -> list[list[tuple[datetime, datetime, str]]]:
    """Split one source's segments into distinct sleep sessions.

    Bucketing raw segments by calendar date does not work: a stretch of sleep
    beginning 23:52 and ending 23:55 shares a date with the *previous* night, so
    a single day's bucket ends up holding the tail of one night and the start of
    the next. That produces a ~24 hour sleep window, a meaningless midpoint and
    a doubled duration. Grouping by continuity first, then dating each session by
    when it ended, keeps every night intact.
    """
    sessions: list[list[tuple[datetime, datetime, str]]] = []
    current: list[tuple[datetime, datetime, str]] = []
    current_end: datetime | None = None

    for seg in sorted(segments, key=lambda x: x[0]):
        start, end, _label = seg
        if current_end is not None:
            gap = (start - current_end).total_seconds() / 60.0
            if gap > SLEEP_SESSION_GAP_MINUTES:
                sessions.append(current)
                current = []
                current_end = None
        current.append(seg)
        if current_end is None or end > current_end:
            current_end = end

    if current:
        sessions.append(current)
    return sessions


def aggregate_sleep_by_day(records: list[HealthRecord]) -> dict[date, dict[str, Any]]:
    """Per-night sleep totals, stages, timing and efficiency.

    Segments are first grouped per source into continuous sessions, then each
    session is attributed to the day it ended on (the wake day). Where several
    sources logged the same night — a sleep tracker, the watch, and the phone's
    in-bed guess — one is chosen rather than summed: staged data (REM/Core/Deep)
    wins, and among equals the one recording the most sleep wins.

    Only the main session of each day is reported, so a daytime nap never
    overwrites the night's timing.

    Deep + core + REM will not always equal the reported total: older watch
    firmware and third-party trackers log AsleepUnspecified, which counts as
    sleep but carries no stage. That remainder is surfaced separately as
    sleep_unspecified_hours rather than quietly breaking the arithmetic.
    """
    by_source: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    seen: set[tuple] = set()

    for rec in records:
        if rec.type != SLEEP_TYPE:
            continue
        label = rec.category_value or ''
        if not label:
            continue
        source = normalize_source(rec.source_name)
        key = (rec.start_raw, rec.end_raw, label, source)
        if key in seen:
            continue
        seen.add(key)
        by_source[source].append((rec.start, rec.end, label))

    candidates: dict[date, list[tuple[bool, float, str, list]]] = defaultdict(list)
    for source, segments in by_source.items():
        for session in _cluster_sleep_sessions(segments):
            asleep = sum(_hours(s, e) for s, e, lbl in session if lbl in ASLEEP_STAGES)
            if asleep <= 0:
                continue  # in-bed-only or awake-only, nothing to report
            staged = any(lbl in STAGED_VALUES for _s, _e, lbl in session)
            wake_day = local_calendar_date(max(e for _s, e, _l in session))
            candidates[wake_day].append((staged, asleep, source, session))

    result: dict[date, dict[str, Any]] = {}
    for day, options in candidates.items():
        staged, _asleep, source, session = max(options, key=lambda o: (o[0], o[1]))
        result[day] = _summarize_night(session, source)
    return result


def _summarize_night(segments: list[tuple[datetime, datetime, str]], source: str) -> dict[str, Any]:
    totals = defaultdict(float)
    asleep_spans: list[tuple[datetime, datetime]] = []
    awake_count = 0

    for start, end, label in segments:
        hours = _hours(start, end)
        if label == SLEEP_IN_BED:
            totals['in_bed'] += hours
        elif label == SLEEP_AWAKE:
            totals['awake'] += hours
            if hours > 0:
                awake_count += 1
        elif label in ASLEEP_STAGES:
            asleep_spans.append((start, end))
            if label == SLEEP_REM:
                totals['rem'] += hours
            elif label == SLEEP_CORE:
                totals['core'] += hours
            elif label == SLEEP_DEEP:
                totals['deep'] += hours
            else:
                totals['unspecified'] += hours

    asleep_total = totals['rem'] + totals['core'] + totals['deep'] + totals['unspecified']

    night: dict[str, Any] = {
        'source': source,
        'in_bed': totals['in_bed'],
        'asleep': asleep_total,
        'rem': totals['rem'],
        'core': totals['core'],
        'deep': totals['deep'],
        'awake': totals['awake'],
        'unspecified': totals['unspecified'],
        'awakenings': awake_count,
    }

    if asleep_spans:
        onset = min(s for s, _e in asleep_spans)
        wake = max(e for _s, e in asleep_spans)
        night['onset'] = onset
        night['wake'] = wake
        # Watch exports usually omit InBed entirely; the onset->wake span is the
        # honest stand-in so efficiency is still computable.
        span = _hours(onset, wake)
        night['window_hours'] = span
        if not night['in_bed'] and span:
            night['in_bed'] = span
        if night['in_bed'] > 0:
            night['efficiency'] = 100.0 * asleep_total / night['in_bed']
        mid = onset + (wake - onset) / 2
        night['midpoint_hour'] = mid.hour + mid.minute / 60.0
    return night


# ---------------------------------------------------------------------------
# Daily assembly
# ---------------------------------------------------------------------------

def build_daily_metrics(data: ExportData) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    used_types: set[str] = set()
    missing_types: set[str] = set()

    sleep_by_day = aggregate_sleep_by_day(data.records)

    # Replace the naive per-day sums for multi-device metrics with de-duplicated
    # totals. Without this, steps/distance/energy are inflated by however much
    # the phone and watch overlap.
    spec_by_column = {spec.column: spec for spec in DAILY_SPECS}
    deduped: dict[str, dict[date, float]] = {}
    for hk_type, column in DEDUP_INTERVAL_TYPES.items():
        spec = spec_by_column.get(column)
        if spec is not None:
            deduped[column] = dedup_interval_sum(data.records, hk_type, spec.convert)
    for hk_type, column in DEDUP_PRIMARY_TYPES.items():
        spec = spec_by_column.get(column)
        if spec is not None:
            deduped[column] = dedup_primary_sum(data.records, hk_type, spec.convert)

    all_days: set[date] = set(data.daily.days) | set(sleep_by_day.keys())
    all_days |= set(data.stand_hours.keys()) | set(data.effort.minutes.keys())
    all_days |= set(data.mindful_minutes.keys()) | set(data.wear.hours.keys())

    rounding = {spec.column: spec.round_to for spec in DAILY_SPECS}

    rows: list[dict[str, Any]] = []
    for day in sorted(all_days):
        row: dict[str, Any] = {'date': day.isoformat()}

        wear_hours = data.wear.wear_hours(day)
        row['wear_hours'] = wear_hours
        row['wear_class'] = WearTracker.classify(wear_hours)

        for spec in DAILY_SPECS:
            if spec.column in deduped:
                val = deduped[spec.column].get(day)
            else:
                val = data.daily.get(day, spec.column, spec.agg)
            if val is None:
                row.setdefault(spec.column, '')
            else:
                digits = rounding.get(spec.column, 2)
                row[spec.column] = round(val, digits) if digits else int(round(val))

        effort_avg = data.effort.average(day)
        row['effort_mets_avg'] = round(effort_avg, 2) if effort_avg is not None else ''
        row['effort_moderate_min'] = round(data.effort.moderate.get(day, 0.0)) or ''
        row['effort_vigorous_min'] = round(data.effort.vigorous.get(day, 0.0)) or ''

        row['stand_hours_met'] = data.stand_hours.get(day, 0) or ''
        mindful = data.mindful_minutes.get(day, 0.0)
        row['mindful_minutes'] = round(mindful, 1) if mindful else ''

        night = sleep_by_day.get(day, {})
        row['sleep_in_bed_hours'] = round(night['in_bed'], 3) if night.get('in_bed') else ''
        asleep = night.get('asleep', 0.0)
        row['sleep_asleep_hours'] = round(asleep, 3) if asleep else ''
        row['sleep_rem_hours'] = round(night['rem'], 3) if night.get('rem') else ''
        row['sleep_core_hours'] = round(night['core'], 3) if night.get('core') else ''
        row['sleep_deep_hours'] = round(night['deep'], 3) if night.get('deep') else ''
        row['sleep_unspecified_hours'] = round(night['unspecified'], 3) if night.get('unspecified') else ''
        row['sleep_awake_hours'] = round(night['awake'], 3) if night.get('awake') else ''
        row['sleep_efficiency_pct'] = round(night['efficiency'], 1) if night.get('efficiency') else ''
        row['sleep_awakenings'] = night.get('awakenings', '') or ''
        row['sleep_deep_pct'] = round(100.0 * night['deep'] / asleep, 1) if asleep and night.get('deep') else ''
        row['sleep_rem_pct'] = round(100.0 * night['rem'] / asleep, 1) if asleep and night.get('rem') else ''
        row['sleep_onset'] = night['onset'].strftime('%H:%M') if night.get('onset') else ''
        row['sleep_wake'] = night['wake'].strftime('%H:%M') if night.get('wake') else ''
        row['sleep_midpoint_hour'] = round(night['midpoint_hour'], 3) if night.get('midpoint_hour') is not None else ''
        row['sleep_source'] = night.get('source', '')

        rows.append(row)

    for t in ALL_PARSED_TYPES:
        if data.type_counts.get(t, 0) > 0:
            used_types.add(t)
        else:
            missing_types.add(t)

    return rows, used_types, missing_types


# ---------------------------------------------------------------------------
# Coverage / validity windows
# ---------------------------------------------------------------------------

def detect_coverage(
    daily_rows: list[dict[str, Any]],
    columns: list[str],
    window: int = 28,
    density: float = 0.5,
) -> list[dict[str, Any]]:
    """Find, per column, the date after which the metric is genuinely tracked.

    A new watch backfills nothing, so a metric's first sample is its true start —
    but an app installed once and abandoned leaves a scatter of early samples
    that would otherwise stretch the analysis window over years of blanks.
    The reliable start is the first date where at least `density` of the next
    `window` days carry a value.
    """
    dates = [date.fromisoformat(r['date']) for r in daily_rows]
    if not dates:
        return []

    out: list[dict[str, Any]] = []
    for col in columns:
        present = [i for i, r in enumerate(daily_rows) if r.get(col) not in ('', None)]
        if not present:
            continue

        first = dates[present[0]]
        last = dates[present[-1]]
        present_dates = {dates[i] for i in present}

        reliable = first
        if col not in SPARSE_BY_DESIGN:
            cursor = first
            end = last
            while cursor <= end:
                # The window only needs `density` of its days filled, so without
                # also requiring the cursor itself to carry a measurement the
                # reported start can precede the metric's first sample by up to
                # half the window.
                if cursor in present_dates:
                    hits = sum(1 for k in range(window)
                               if (cursor + timedelta(days=k)) in present_dates)
                    if hits >= window * density:
                        break
                cursor += timedelta(days=1)
            reliable = cursor if cursor <= end else first

        span_days = (last - reliable).days + 1
        covered = sum(1 for d in present_dates if d >= reliable)
        group, label = COLUMN_META.get(col, ('other', col))
        out.append({
            'column': col,
            'group': group,
            'label': label,
            'first_date': first.isoformat(),
            'reliable_start': reliable.isoformat(),
            'last_date': last.isoformat(),
            'days_with_data': len(present_dates),
            'coverage_pct_since_reliable_start': round(100.0 * covered / span_days, 1) if span_days > 0 else '',
            'sparse_by_design': 'yes' if col in SPARSE_BY_DESIGN else 'no',
        })
    return out


def analysis_start_date(coverage: list[dict[str, Any]], anchors: Iterable[str]) -> date | None:
    """The date from which the continuously-tracked core metrics are all live."""
    starts = [
        date.fromisoformat(c['reliable_start'])
        for c in coverage
        if c['column'] in set(anchors)
    ]
    return max(starts) if starts else None


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------

def hr_zone_for_bpm(bpm: float, max_hr: float) -> str:
    pct = bpm / max_hr
    for bound, zone in HR_ZONE_BOUNDS:
        if pct < bound:
            return zone
    return 'z5'


def heart_rate_samples(data: ExportData) -> list[HealthRecord]:
    return [r for r in data.records if r.type == 'HKQuantityTypeIdentifierHeartRate' and r.value is not None]


def cycling_metric_samples(data: ExportData, metric_type: str) -> list[HealthRecord]:
    return [r for r in data.records if r.type == metric_type and r.value is not None]


# A workout peak this far above every other observation is treated as a sensor
# artifact rather than a real maximum effort.
MAX_HR_OUTLIER_GAP = 10.0


def observed_max_hr(
    hr_samples: list[HealthRecord],
    workouts: list[Workout] | None = None,
) -> tuple[float | None, float | None, str]:
    """Best estimate of true max HR, plus the raw absolute peak and a note.

    Workouts carry their own `WorkoutStatistics maximum`, often higher than any
    retained sample because the periodic sample stream misses the true peak of a
    session. But wrist optical sensors also throw artifacts — motion and cadence
    lock during strength work can invent a spike tens of bpm above reality — and
    since max HR sets every zone boundary, one bad reading would distort the
    entire training analysis. So an isolated peak standing more than
    MAX_HR_OUTLIER_GAP above the next-best observation is discarded.
    """
    sample_values = [s.value for s in hr_samples if s.value is not None]
    sample_max = max(sample_values, default=None)

    workout_maxima = sorted(
        (peak for w in (workouts or [])
         if (peak := stat_value(w, 'HKQuantityTypeIdentifierHeartRate', 'maximum')) is not None),
        reverse=True,
    )

    candidates = sample_values + workout_maxima
    if not candidates:
        return None, None, ''
    absolute = max(candidates)

    if not workout_maxima:
        return absolute, absolute, ''

    top = workout_maxima[0]
    corroboration = ([sample_max] if sample_max is not None else []) + workout_maxima[1:]
    runner_up = max(corroboration, default=None)
    if runner_up is not None and top - runner_up > MAX_HR_OUTLIER_GAP:
        note = (f'ignored an isolated workout peak of {top:.0f} bpm — no other sample or '
                f'workout exceeded {runner_up:.0f} bpm, so it reads as a sensor artifact')
        return runner_up, absolute, note

    return absolute, absolute, ''


def resolve_max_hr(
    hr_samples: list[HealthRecord],
    configured: float | None = None,
    age: int | None = None,
    workouts: list[Workout] | None = None,
) -> tuple[float, str]:
    """Pick a max HR for zone maths, preferring evidence over a fixed default."""
    if configured:
        return float(configured), 'configured via --max-hr'

    observed, absolute, outlier_note = observed_max_hr(hr_samples, workouts)
    suffix = f'; {outlier_note}' if outlier_note else ''

    if age:
        predicted = 208 - 0.7 * age
        if observed and observed > predicted:
            return float(observed), (f'highest corroborated HR ({observed:.0f} bpm), above the '
                                     f'age prediction of {predicted:.0f}{suffix}')
        return float(predicted), f'age-predicted (208 - 0.7 x {age}){suffix}'
    if observed:
        note = f'highest corroborated heart rate in export ({observed:.0f} bpm){suffix}'
        if not outlier_note and absolute is not None:
            note = f'highest heart rate observed in export ({absolute:.0f} bpm)'
        return float(observed), note
    return DEFAULT_MAX_HR, 'library default (no heart-rate data found)'


def overlap_minutes(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        if a_start == a_end:
            return 1.0 / 60.0
        return 0.0
    return (end - start).total_seconds() / 60.0


def compute_workout_hr_zones(
    workout: Workout,
    hr_samples: list[HealthRecord],
    max_hr: float = DEFAULT_MAX_HR,
) -> dict[str, Any]:
    in_window = [s for s in hr_samples if s.start < workout.end and s.end > workout.start]
    in_window.sort(key=lambda s: s.start)

    zone_mins = {f'hr_zone_{z}_min': 0.0 for _, z in HR_ZONE_BOUNDS}
    bpm_values: list[float] = []

    for i, sample in enumerate(in_window):
        bpm = sample.value
        if bpm is None:
            continue
        bpm_values.append(bpm)
        if i + 1 < len(in_window):
            next_start = in_window[i + 1].start
            seg_end = min(sample.end, next_start, workout.end)
        else:
            seg_end = workout.end
        seg_start = max(sample.start, workout.start)
        minutes = overlap_minutes(seg_start, seg_end, workout.start, workout.end)
        if minutes <= 0:
            minutes = 1.0 / 60.0
        minutes = min(minutes, 5.0)
        zone = hr_zone_for_bpm(bpm, max_hr)
        zone_mins[f'hr_zone_{zone}_min'] += minutes

    result: dict[str, Any] = {
        'avg_heart_rate_bpm': round(statistics.mean(bpm_values), 1) if bpm_values else '',
        'max_heart_rate_bpm': round(max(bpm_values), 1) if bpm_values else '',
    }
    for key, val in zone_mins.items():
        result[key] = round(val, 2) if val > 0 else ''
    return result


def stat_value(workout: Workout, stat_type: str, field_name: str = 'average') -> float | None:
    stat = workout.statistics.get(stat_type, {})
    return stat.get(field_name)


def metrics_in_workout_window(workout: Workout, samples: list[HealthRecord]) -> list[float]:
    return [
        s.value for s in samples
        if s.start < workout.end and s.end > workout.start and s.value is not None
    ]


def build_workout_summary(data: ExportData, max_hr: float = DEFAULT_MAX_HR) -> list[dict[str, Any]]:
    hr_samples = heart_rate_samples(data)
    power_samples = cycling_metric_samples(data, 'HKQuantityTypeIdentifierCyclingPower')
    cadence_samples = cycling_metric_samples(data, 'HKQuantityTypeIdentifierCyclingCadence')

    rows: list[dict[str, Any]] = []
    for workout in sorted(data.workouts, key=lambda w: w.start):
        duration_min = duration_to_minutes(workout.duration, workout.duration_unit)
        if duration_min is None:
            duration_min = (workout.end - workout.start).total_seconds() / 60.0

        distance_km = distance_to_km(workout.total_distance, workout.total_distance_unit)
        if distance_km is None:
            dist_stat = stat_value(workout, 'HKQuantityTypeIdentifierDistanceCycling', 'sum')
            if dist_stat is None:
                dist_stat = stat_value(workout, 'HKQuantityTypeIdentifierDistanceWalkingRunning', 'sum')
            distance_km = distance_to_km(
                dist_stat,
                workout.statistics.get('HKQuantityTypeIdentifierDistanceCycling', {}).get('unit', 'km'),
            )

        total_kcal = energy_to_kcal(workout.total_energy, workout.total_energy_unit)
        active_kcal = energy_to_kcal(
            stat_value(workout, 'HKQuantityTypeIdentifierActiveEnergyBurned', 'sum'),
            workout.statistics.get('HKQuantityTypeIdentifierActiveEnergyBurned', {}).get('unit', 'kcal'),
        )

        hr_stats = compute_workout_hr_zones(workout, hr_samples, max_hr)
        if hr_stats['avg_heart_rate_bpm'] == '':
            avg_stat = stat_value(workout, 'HKQuantityTypeIdentifierHeartRate', 'average')
            max_stat = stat_value(workout, 'HKQuantityTypeIdentifierHeartRate', 'maximum')
            if avg_stat is not None:
                hr_stats['avg_heart_rate_bpm'] = round(avg_stat, 1)
            if max_stat is not None:
                hr_stats['max_heart_rate_bpm'] = round(max_stat, 1)

        power_vals = metrics_in_workout_window(workout, power_samples)
        cadence_vals = metrics_in_workout_window(workout, cadence_samples)
        power_avg = stat_value(workout, 'HKQuantityTypeIdentifierCyclingPower', 'average')
        power_max = stat_value(workout, 'HKQuantityTypeIdentifierCyclingPower', 'maximum')
        cadence_avg = stat_value(workout, 'HKQuantityTypeIdentifierCyclingCadence', 'average')

        rows.append({
            'workout_id': workout_id_for(workout),
            'activity_type': friendly_workout_type(workout.workout_activity_type),
            'date': local_calendar_date(workout.start).isoformat(),
            'start_datetime': workout.start_raw,
            'end_datetime': workout.end_raw,
            'duration_min': round(duration_min, 2) if duration_min is not None else '',
            'total_energy_kcal': round(total_kcal, 2) if total_kcal is not None else '',
            'active_energy_kcal': round(active_kcal, 2) if active_kcal is not None else '',
            'distance_km': round(distance_km, 3) if distance_km is not None else '',
            'avg_heart_rate_bpm': hr_stats['avg_heart_rate_bpm'],
            'max_heart_rate_bpm': hr_stats['max_heart_rate_bpm'],
            'hr_zone_z1_min': hr_stats['hr_zone_z1_min'],
            'hr_zone_z2_min': hr_stats['hr_zone_z2_min'],
            'hr_zone_z3_min': hr_stats['hr_zone_z3_min'],
            'hr_zone_z4_min': hr_stats['hr_zone_z4_min'],
            'hr_zone_z5_min': hr_stats['hr_zone_z5_min'],
            'cycling_power_avg_w': round(statistics.mean(power_vals), 1)
            if power_vals else (round(power_avg, 1) if power_avg is not None else ''),
            'cycling_power_max_w': round(max(power_vals), 1)
            if power_vals else (round(power_max, 1) if power_max is not None else ''),
            'cycling_cadence_avg_rpm': round(statistics.mean(cadence_vals), 1)
            if cadence_vals else (round(cadence_avg, 1) if cadence_avg is not None else ''),
            'source_name': workout.source_name,
        })

    return rows


# ---------------------------------------------------------------------------
# Weekly
# ---------------------------------------------------------------------------

def iso_week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f'{year}-W{week:02d}'


WEEKLY_MEAN_COLUMNS = [
    ('sleep_asleep_hours', 'avg_sleep_hours'),
    ('sleep_deep_hours', 'avg_deep_sleep_hours'),
    ('sleep_rem_hours', 'avg_rem_sleep_hours'),
    ('sleep_efficiency_pct', 'avg_sleep_efficiency_pct'),
    ('resting_hr', 'avg_resting_hr'),
    ('hrv_sdnn', 'avg_hrv_sdnn'),
    ('respiratory_rate', 'avg_respiratory_rate'),
    ('spo2_avg', 'avg_spo2_pct'),
    ('wrist_temp_c', 'avg_wrist_temp_c'),
    ('body_mass_kg', 'weight_week_avg'),
    ('body_fat_pct', 'body_fat_week_avg'),
    ('walking_speed_kmh', 'avg_walking_speed_kmh'),
    ('vo2max', 'vo2max_week_avg'),
    ('wear_hours', 'avg_wear_hours'),
]

WEEKLY_SUM_COLUMNS = [
    ('steps', 'total_steps'),
    ('active_kcal', 'total_active_kcal'),
    ('exercise_minutes', 'total_exercise_minutes'),
    ('effort_moderate_min', 'total_moderate_effort_min'),
    ('effort_vigorous_min', 'total_vigorous_effort_min'),
    ('daylight_minutes', 'total_daylight_minutes'),
    ('diet_protein_g', 'total_protein_g'),
]


def build_weekly_summary(
    daily_rows: list[dict[str, Any]],
    workouts: list[Workout],
    workout_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    weeks: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for row in daily_rows:
        if not row.get('date'):
            continue
        wk = iso_week_key(date.fromisoformat(row['date']))
        bucket = weeks[wk]
        for src, _dest in WEEKLY_MEAN_COLUMNS + WEEKLY_SUM_COLUMNS:
            val = row.get(src)
            if val not in ('', None):
                with contextlib.suppress(TypeError, ValueError):
                    bucket[src].append(float(val))
        if row.get('wear_class') == 'full':
            bucket['_full_wear_days'].append(1.0)

    workout_weeks: dict[str, list[Workout]] = defaultdict(list)
    for w in workouts:
        workout_weeks[iso_week_key(local_calendar_date(w.start))].append(w)

    workout_row_by_id = {r['workout_id']: r for r in workout_rows}
    all_weeks = sorted(set(weeks.keys()) | set(workout_weeks.keys()))

    rows: list[dict[str, Any]] = []
    for wk in all_weeks:
        wlist = workout_weeks.get(wk, [])
        b = weeks.get(wk, {})

        z2_total = 0.0
        for w in wlist:
            wrow = workout_row_by_id.get(workout_id_for(w), {})
            z2 = wrow.get('hr_zone_z2_min')
            if z2 not in ('', None):
                z2_total += float(z2)

        row: dict[str, Any] = {
            'iso_week': wk,
            'full_wear_days': int(sum(b.get('_full_wear_days', []))) or '',
            'workouts_total': len(wlist) if wlist else '',
            'strength_sessions': sum(1 for w in wlist if w.workout_activity_type in STRENGTH_WORKOUT_TYPES) or '',
            'cycling_sessions': sum(1 for w in wlist if w.workout_activity_type in CYCLING_WORKOUT_TYPES) or '',
            'walking_sessions': sum(1 for w in wlist if w.workout_activity_type in WALKING_WORKOUT_TYPES) or '',
            'zone2_minutes_estimate': round(z2_total, 1) if z2_total else '',
        }
        for src, dest in WEEKLY_SUM_COLUMNS:
            vals = b.get(src, [])
            row[dest] = round(sum(vals), 1) if vals else ''
        for src, dest in WEEKLY_MEAN_COLUMNS:
            vals = b.get(src, [])
            row[dest] = round(statistics.mean(vals), 2) if vals else ''
        rows.append(row)

    return rows


def weekly_column_order() -> list[str]:
    cols = ['iso_week', 'full_wear_days', 'workouts_total', 'strength_sessions',
            'cycling_sessions', 'walking_sessions', 'zone2_minutes_estimate']
    cols += [dest for _src, dest in WEEKLY_SUM_COLUMNS]
    cols += [dest for _src, dest in WEEKLY_MEAN_COLUMNS]
    return cols


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_csv(path: str, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_available_types(path: str, type_counts: Counter) -> None:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['type', 'count', 'used_in_coaching_outputs'])
        for t, count in sorted(type_counts.items(), key=lambda x: (-x[1], x[0])):
            used = t in ALL_PARSED_TYPES or t.startswith('Workout:') or t == 'ActivitySummary'
            writer.writerow([t, count, 'yes' if used else 'no'])


def empty_ratio(rows: list[dict[str, Any]], column: str) -> float:
    if not rows:
        return 1.0
    empty = sum(1 for r in rows if r.get(column) in ('', None))
    return empty / len(rows)


def detect_duplicate_bursts(records: list[HealthRecord], threshold: int = 50) -> list[str]:
    by_minute: Counter = Counter()
    for rec in records:
        by_minute[(rec.type, rec.start.replace(second=0, microsecond=0))] += 1
    bursts = [(k, c) for k, c in by_minute.items() if c >= threshold]
    lines = []
    for (rtype, minute), count in sorted(bursts, key=lambda x: -x[1])[:10]:
        lines.append(f"  {rtype} @ {minute.isoformat()}: {count} records in one minute")
    return lines


def summarize_wear(daily_rows: list[dict[str, Any]], since: date | None) -> dict[str, int]:
    counts = Counter()
    for row in daily_rows:
        d = date.fromisoformat(row['date'])
        if since and d < since:
            continue
        counts[row.get('wear_class', 'none')] += 1
    return counts


def write_data_quality_report(
    path: str,
    daily_rows: list[dict[str, Any]],
    workout_rows: list[dict[str, Any]],
    weekly_rows: list[dict[str, Any]],
    data: ExportData,
    coverage: list[dict[str, Any]],
    analysis_start: date | None,
    max_hr: float,
    max_hr_note: str,
    tz_label: str,
) -> None:
    all_dates = [date.fromisoformat(r['date']) for r in daily_rows if r.get('date')]
    date_range = ''
    if all_dates:
        date_range = f"{min(all_dates).isoformat()} to {max(all_dates).isoformat()} ({len(all_dates)} days)"

    lines = [
        'Apple Health Coaching Data Quality Report',
        '=' * 60,
        f'Timezone handling: dates are the local wall-clock date recorded in each sample (sample offset: {tz_label})',
        f'Date range in export: {date_range or "n/a"}',
        f'Analysis window starts: {analysis_start.isoformat() if analysis_start else "n/a"}'
        ' (first date where the continuously-tracked core metrics are all live)',
        f'Max heart rate used for zones: {max_hr:.0f} bpm — {max_hr_note}',
        '',
    ]

    wear = summarize_wear(daily_rows, analysis_start)
    total_wear_days = sum(wear.values())
    lines.append('Watch wear inside the analysis window:')
    if total_wear_days:
        for cls in ('full', 'partial', 'minimal', 'none'):
            n = wear.get(cls, 0)
            lines.append(f'  {cls:<8} {n:>5} days ({100.0 * n / total_wear_days:5.1f}%)')
    else:
        lines.append('  n/a')
    lines.append(f'  thresholds: full >= {WEAR_FULL_HOURS}h, partial >= {WEAR_PARTIAL_HOURS}h, minimal > 0h')
    lines.append('')

    lines.append('Per-metric coverage (reliable_start = first date with sustained tracking):')
    lines.append(f"  {'column':<32} {'first':<11} {'reliable':<11} {'last':<11} {'days':>6} {'cov%':>6}")
    for c in sorted(coverage, key=lambda x: (x['group'], x['column'])):
        lines.append(
            f"  {c['column']:<32} {c['first_date']:<11} {c['reliable_start']:<11} "
            f"{c['last_date']:<11} {c['days_with_data']:>6} {c['coverage_pct_since_reliable_start']:>6}"
        )
    lines.append('')
    lines.append('  (metrics marked sparse-by-design — weight, VO2 max, body fat — are measured')
    lines.append('   occasionally rather than continuously; blank days are expected, not missing.)')

    tracked = [c for c in coverage if c['sparse_by_design'] == 'no']
    lines.extend(['', 'Missingness inside each metric\'s own valid window (% of days blank):'])
    for c in sorted(tracked, key=lambda x: -float(x['coverage_pct_since_reliable_start'] or 0)):
        gap = 100.0 - float(c['coverage_pct_since_reliable_start'] or 0)
        lines.append(f"  {c['column']:<32} {gap:5.1f}%   since {c['reliable_start']}")

    workout_cols = ['duration_min', 'total_energy_kcal', 'distance_km', 'avg_heart_rate_bpm',
                    'hr_zone_z2_min', 'cycling_power_avg_w']
    lines.extend(['', 'Missingness — workout_summary.csv (% empty):'])
    for col in workout_cols:
        lines.append(f'  {col}: {empty_ratio(workout_rows, col) * 100:.1f}%')

    # Body composition read from two different scales is not a trend, it is two
    # different instruments disagreeing. Bioimpedance devices routinely differ by
    # several percentage points on the same body, so this needs calling out
    # before anyone reads a device swap as fat loss.
    multi = {
        col: srcs for col, srcs in data.metric_sources.items()
        if len(srcs) > 1 and col in ('body_fat_pct', 'body_mass_kg', 'lean_body_mass_kg',
                                     'bmi', 'diet_kcal', 'diet_protein_g')
    }
    if multi:
        lines.extend(['', 'Metrics recorded by more than one device or app:'])
        for col, srcs in sorted(multi.items()):
            breakdown = ', '.join(f'{s} ({n})' for s, n in srcs.most_common())
            lines.append(f'  {col}: {breakdown}')
        lines.append('  Cross-device comparisons here are unreliable — different scales and')
        lines.append('  food-logging apps are offset from each other, so a change in value may')
        lines.append('  simply be a change in instrument.')

    lines.extend(['', 'Data sources seen (normalized):'])
    for src, count in data.sources.most_common(15):
        lines.append(f'  {count:>9,}  {src}')

    lines.extend(['', f'Outputs: {len(daily_rows)} daily rows, {len(workout_rows)} workouts, {len(weekly_rows)} weeks'])

    burst_lines = detect_duplicate_bursts(data.records)
    lines.append('')
    lines.append('Duplicate bursts (>=50 records of one type in a single minute):')
    lines.extend(burst_lines if burst_lines else ['  none detected'])

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def records_to_legacy_rows(data: ExportData) -> list[dict[str, str]]:
    """Backward-compatible flat rows for full_health_data.csv."""
    rows: list[dict[str, str]] = []

    for rec in data.records:
        if rec.type not in TARGET_TYPES:
            continue
        val = rec.category_value if rec.category_value else ('' if rec.value is None else str(rec.value))
        rows.append({
            'creationDate': rec.creation,
            'startDate': rec.start_raw,
            'endDate': rec.end_raw,
            'type': rec.type,
            'value': val,
        })

    for workout in data.workouts:
        value_parts = []
        if workout.duration is not None:
            value_parts.append(f"duration:{workout.duration} {workout.duration_unit}")
        if workout.total_energy is not None:
            value_parts.append(f"calories:{workout.total_energy} {workout.total_energy_unit}")
        rows.append({
            'creationDate': workout.creation,
            'startDate': workout.start_raw,
            'endDate': workout.end_raw,
            'type': workout.workout_activity_type,
            'value': '; '.join(value_parts),
        })

    rows.sort(key=lambda r: r['startDate'])
    return rows


# Core continuously-tracked metrics that define the usable analysis window.
ANALYSIS_ANCHORS = ('resting_hr', 'hrv_sdnn', 'sleep_asleep_hours')


@dataclass
class CoachingResult:
    paths: dict[str, str]
    daily_rows: list[dict[str, Any]]
    coverage: list[dict[str, Any]]
    analysis_start: date | None
    max_hr: float


def write_coaching_outputs(
    base_dir: str,
    data: ExportData,
    max_hr_override: float | None = None,
    age: int | None = None,
) -> CoachingResult:
    data.timezone_label = infer_timezone_label(data.records, data.workouts)

    hr_samples = heart_rate_samples(data)
    max_hr, max_hr_note = resolve_max_hr(hr_samples, max_hr_override, age, data.workouts)
    print(f"  Max heart rate for zones: {max_hr:.0f} bpm ({max_hr_note})")

    daily_rows, _used, _missing = build_daily_metrics(data)
    workout_rows = build_workout_summary(data, max_hr)
    weekly_rows = build_weekly_summary(daily_rows, data.workouts, workout_rows)

    daily_cols = daily_column_order()
    tracked_cols = [c for c in daily_cols if c not in ('date', 'wear_class', 'sleep_source',
                                                       'sleep_onset', 'sleep_wake')]
    coverage = detect_coverage(daily_rows, tracked_cols)
    start = analysis_start_date(coverage, ANALYSIS_ANCHORS)
    if start:
        print(f"  Analysis window starts {start.isoformat()} (auto-detected from metric coverage)")

    paths = {
        'daily_metrics': os.path.join(base_dir, 'daily_metrics.csv'),
        'workout_summary': os.path.join(base_dir, 'workout_summary.csv'),
        'weekly_summary': os.path.join(base_dir, 'weekly_summary.csv'),
        'metric_coverage': os.path.join(base_dir, 'metric_coverage.csv'),
        'available_types': os.path.join(base_dir, 'available_types.csv'),
        'data_quality_report': os.path.join(base_dir, 'data_quality_report.txt'),
    }

    write_csv(paths['daily_metrics'], daily_cols, daily_rows)
    write_csv(
        paths['workout_summary'],
        ['workout_id', 'activity_type', 'date', 'start_datetime', 'end_datetime', 'duration_min',
         'total_energy_kcal', 'active_energy_kcal', 'distance_km', 'avg_heart_rate_bpm',
         'max_heart_rate_bpm', 'hr_zone_z1_min', 'hr_zone_z2_min', 'hr_zone_z3_min',
         'hr_zone_z4_min', 'hr_zone_z5_min', 'cycling_power_avg_w', 'cycling_power_max_w',
         'cycling_cadence_avg_rpm', 'source_name'],
        workout_rows,
    )
    write_csv(paths['weekly_summary'], weekly_column_order(), weekly_rows)
    write_csv(
        paths['metric_coverage'],
        ['column', 'group', 'label', 'first_date', 'reliable_start', 'last_date',
         'days_with_data', 'coverage_pct_since_reliable_start', 'sparse_by_design'],
        coverage,
    )
    write_available_types(paths['available_types'], data.type_counts)
    write_data_quality_report(
        paths['data_quality_report'], daily_rows, workout_rows, weekly_rows,
        data, coverage, start, max_hr, max_hr_note, data.timezone_label,
    )

    return CoachingResult(
        paths=paths,
        daily_rows=daily_rows,
        coverage=coverage,
        analysis_start=start,
        max_hr=max_hr,
    )
