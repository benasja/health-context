"""Read Apple Health data from a Health Bridge receiver instead of an export.

Health Bridge for AI (https://github.com/roian6/apple-health-ai-bridge) syncs
Apple Health from the iPhone to a SQLite database on a machine you own, so new
data arrives on its own instead of waiting for the next manual export. This
module reads that database and feeds it through exactly the same pipeline as
export.xml, so every baseline, score and flag downstream is computed the same
way whichever input it came from.

It never writes to the receiver's database: the connection is opened
read-only, and only the tables holding health records, and the two lookups
they refer to, are read. The pairing and credential tables are never touched.

What has to be translated, and why each one matters:

* **Time.** The bridge stores UTC. Everything here works on the local calendar
  day -- a night's sleep belongs to the morning you woke up, not to whatever
  UTC said -- so timestamps are converted to the user's timezone first. A
  receiver running in Docker is usually set to UTC, so this does not trust the
  machine's own zone: it uses the zone the phone reported, unless told
  otherwise.
* **Daily totals.** For summed metrics (steps, energy, exercise minutes...) the
  phone sends Apple's own per-day total, already de-duplicated across iPhone
  and Watch, and sometimes the raw samples as well. Feeding both would let the
  raw Watch samples win the interval de-duplication and the merged total be
  thrown away. Where a day has the phone's total, the raw samples for that type
  and day are skipped -- the same rule the receiver's own daily summary uses.
* **Units.** The bridge sends percentages as 0-100; Apple's export, which this
  project was built against, writes fractions. Left alone, SpO2 would read
  9,700%. Heart rate arrives as "bpm", minutes as "minutes", walking speed in
  m/s against a column that is km/h. All of it is converted to what export.xml
  would have said, so the rest of the code sees nothing new.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone, tzinfo

from health_coaching import (
    ALL_PARSED_TYPES,
    SLEEP_AWAKE,
    SLEEP_CORE,
    SLEEP_DEEP,
    SLEEP_IN_BED,
    SLEEP_REM,
    SLEEP_TYPE,
    STAND_HOUR_TYPE,
    ExportData,
    Workout,
    add_parsed_record,
)

# The tables and columns read here. Anything missing means the receiver's
# schema changed in a way this code was not written for, and the right answer
# is to stop and say so -- not to guess at a renamed column with health data.
REQUIRED_COLUMNS: dict[str, set[str]] = {
    'sources': {'source_id', 'name', 'kind'},
    'health_type_aliases': {'type_code', 'alias'},
    'samples': {'source_id', 'type_code', 'client_record_id', 'start_time', 'end_time',
                'value', 'unit', 'metadata_json', 'created_at'},
    'workouts': {'source_id', 'workout_type', 'start_time', 'end_time', 'duration_seconds',
                 'energy_kcal', 'distance_meters', 'created_at'},
    'sleep_sessions': {'sleep_session_id', 'source_id', 'start_time', 'end_time', 'created_at'},
    'sleep_stage_intervals': {'sleep_session_id', 'stage', 'start_time', 'end_time'},
}

STAGE_TO_EXPORT = {
    'in_bed': SLEEP_IN_BED,
    'awake': SLEEP_AWAKE,
    'core': SLEEP_CORE,
    'deep': SLEEP_DEEP,
    'rem': SLEEP_REM,
}

# The companion app's names for workout types, mapped back to HealthKit's.
# Anything else arrives as "other", which is what HealthKit calls it too.
WORKOUT_TO_EXPORT = {
    'running': 'HKWorkoutActivityTypeRunning',
    'walking': 'HKWorkoutActivityTypeWalking',
    'cycling': 'HKWorkoutActivityTypeCycling',
    'swimming': 'HKWorkoutActivityTypeSwimming',
    'hiking': 'HKWorkoutActivityTypeHiking',
    'yoga': 'HKWorkoutActivityTypeYoga',
    'traditional_strength_training': 'HKWorkoutActivityTypeTraditionalStrengthTraining',
    'functional_strength_training': 'HKWorkoutActivityTypeFunctionalStrengthTraining',
    'hiit': 'HKWorkoutActivityTypeHighIntensityIntervalTraining',
    'rowing': 'HKWorkoutActivityTypeRowing',
    'elliptical': 'HKWorkoutActivityTypeElliptical',
    'pilates': 'HKWorkoutActivityTypePilates',
    'core_training': 'HKWorkoutActivityTypeCoreTraining',
    'dance': 'HKWorkoutActivityTypeDance',
    'mind_and_body': 'HKWorkoutActivityTypeMindAndBody',
    'other': 'HKWorkoutActivityTypeOther',
}

# Which distance statistic a workout's total distance belongs to, so the
# per-activity distance logic downstream finds it where an export puts it.
WORKOUT_DISTANCE_STAT = {
    'running': 'HKQuantityTypeIdentifierDistanceWalkingRunning',
    'walking': 'HKQuantityTypeIdentifierDistanceWalkingRunning',
    'hiking': 'HKQuantityTypeIdentifierDistanceWalkingRunning',
    'cycling': 'HKQuantityTypeIdentifierDistanceCycling',
    'swimming': 'HKQuantityTypeIdentifierDistanceSwimming',
}

# Unit names the companion app uses -> the names Apple's export uses.
UNIT_TO_EXPORT = {
    'bpm': 'count/min',
    'brpm': 'count/min',
    'minutes': 'min',
    '°C': 'degC',
    'liters': 'L',
    'watts': 'W',
}

DAILY_TOTAL_SOURCE = 'Apple Health daily total'
EXPORT_TIME_FORMAT = '%Y-%m-%d %H:%M:%S %z'


class BridgeError(Exception):
    """The database is missing, unreadable, or not a schema this understands."""


@dataclass
class BridgeSummary:
    """What was read, and what was deliberately left out, for the run's printout."""

    timezone_name: str = ''
    timezone_reason: str = ''
    samples: int = 0
    daily_totals: int = 0
    raw_replaced_by_daily_total: int = 0
    sleep_stages: int = 0
    workouts: int = 0
    unmapped_types: Counter = field(default_factory=Counter)
    not_parsed_types: Counter = field(default_factory=Counter)


def open_readonly(path: str) -> sqlite3.Connection:
    """Open the receiver's database without any possibility of writing to it.

    `mode=ro` is enforced by SQLite itself, so even a bug here cannot alter the
    receiver's data. A missing file is reported plainly rather than letting
    SQLite create an empty database in its place, which is what a plain
    `connect()` would do.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise BridgeError(f'No Health Bridge database at {path}')
    try:
        # as_uri() percent-encodes the path: a raw '#' or '?' in a folder name
        # would otherwise end the filename early and open something else.
        conn = sqlite3.connect(f'{pathlib.Path(path).as_uri()}?mode=ro', uri=True, timeout=30)
        conn.execute('select 1 from sqlite_master limit 1')
    except sqlite3.DatabaseError as exc:
        if in_wal_mode(path):
            raise BridgeError(
                f'Cannot open {path}: it is in WAL mode, and SQLite has to write a small '
                f'{os.path.basename(path)}-shm file beside it even to read it. Give this '
                'process write access to that folder -- under Docker, mount it :rw instead '
                'of :ro. The database itself is still opened read-only.') from exc
        raise BridgeError(f'{path} is not a readable SQLite database: {exc}') from exc
    return conn


def in_wal_mode(path: str) -> bool:
    """Whether a database file says it is in WAL mode, without opening it.

    Bytes 18 and 19 of the header are the file format's write and read
    versions: 1 for the default rollback journal, 2 for WAL. Reading them
    directly works even when SQLite itself cannot open the file -- which is
    exactly the case this is for: a WAL database in a read-only folder.
    """
    try:
        with open(path, 'rb') as handle:
            header = handle.read(20)
    except OSError:
        return False
    return header[:16] == b'SQLite format 3\x00' and header[18:20] == b'\x02\x02'


def check_schema(conn: sqlite3.Connection) -> None:
    problems: list[str] = []
    for table, needed in REQUIRED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f'pragma table_info({table})')}
        if not present:
            problems.append(f'table {table} is missing')
        elif needed - present:
            problems.append(f'{table} lacks {", ".join(sorted(needed - present))}')
    if problems:
        raise BridgeError(
            'This does not look like a Health Bridge database this version can read ('
            + '; '.join(problems)
            + '). If the receiver was upgraded, health-context may need updating too.')


def parse_utc(raw: str) -> datetime:
    """The bridge's timestamps: ISO 8601 in UTC, written with a trailing Z."""
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _metadata(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_daily_total(meta: dict) -> bool:
    # The same test the receiver's own daily summary applies.
    return meta.get('aggregation') == 'daily_sum' or meta.get('healthkit_query') == 'HKStatisticsCollectionQuery'


def resolve_timezone(conn: sqlite3.Connection, requested: str | None) -> tuple[tzinfo, str, str]:
    """The zone local days are measured in, and why that one was chosen.

    Order: an explicit --timezone; else the zone the phone reported on its
    daily totals (the most common one, so a holiday does not flip the whole
    history); else this machine's zone, with a warning in the reason, because
    in a container that is usually UTC and would shift every evening into the
    next day.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def load(name: str) -> tzinfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise BridgeError(
                f'Unknown timezone {name!r}. Use an IANA name such as Europe/Vilnius. '
                'In a slim container image the timezone database may be missing: '
                'install the tzdata package.') from exc

    if requested:
        return load(requested), requested, 'set with --timezone'

    seen: Counter = Counter()
    for (raw,) in conn.execute(
            "select metadata_json from samples where metadata_json like '%time_zone_identifier%' limit 20000"):
        name = _metadata(raw).get('time_zone_identifier')
        if isinstance(name, str) and name:
            seen[name] += 1
    if seen:
        name = seen.most_common(1)[0][0]
        return load(name), name, 'reported by the phone'

    local = datetime.now().astimezone().tzinfo or timezone.utc
    label = getattr(local, 'key', None) or str(local)
    return local, label, ("this machine's zone -- the phone reported none. If the receiver runs "
                          "in a container on UTC, pass --timezone or every evening lands a day late")


SPEED_TYPES = {'HKQuantityTypeIdentifierWalkingSpeed', 'HKQuantityTypeIdentifierCyclingSpeed'}

# HKCategoryValueAppleStandHour as HealthKit numbers it. export.xml spells the
# value out instead, and the stand-hour count only reads the spelling.
STAND_HOUR_VALUES = {0: 'HKCategoryValueAppleStandHourStood', 1: 'HKCategoryValueAppleStandHourIdle'}


def to_export_value(hk_type: str, value: float, unit: str) -> tuple[float, str]:
    """Convert a bridge value into the number and unit export.xml would hold."""
    if unit == '%':
        return value / 100.0, '%'
    if hk_type in SPEED_TYPES and unit == 'm/s':
        return value * 3.6, 'km/hr'
    return value, UNIT_TO_EXPORT.get(unit, unit)


def stand_hour_label(value: float) -> str | None:
    """The export's label for a stand hour sent as HealthKit's number.

    Passed through as a number it would read as "0.0" or "1.0", and the stand
    count treats anything without "idle" in it as a stood hour: every idle hour
    would count as stood. An unknown number is dropped rather than guessed.
    """
    return STAND_HOUR_VALUES.get(int(value)) if float(value).is_integer() else None


def _stamp(dt: datetime) -> str:
    return dt.strftime(EXPORT_TIME_FORMAT)


def load_into(data: ExportData, db_path: str, timezone_name: str | None = None) -> BridgeSummary:
    """Read a Health Bridge database into `data`. Raises BridgeError on a bad DB."""
    conn = open_readonly(db_path)
    try:
        check_schema(conn)
        tz, tz_name, tz_reason = resolve_timezone(conn, timezone_name)
        summary = BridgeSummary(timezone_name=tz_name, timezone_reason=tz_reason)
        aliases = _healthkit_aliases(conn)
        _load_samples(conn, data, tz, aliases, summary)
        _load_sleep(conn, data, tz, summary)
        _load_workouts(conn, data, tz, summary)
        data.timezone_label = tz_name
        return summary
    finally:
        conn.close()


def _healthkit_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """The receiver's own map from its type codes to HealthKit identifiers."""
    aliases: dict[str, str] = {}
    for type_code, alias in conn.execute(
            "select type_code, alias from health_type_aliases where alias like 'HK%Identifier%'"):
        aliases.setdefault(type_code, alias)
    return aliases


def _local_day_span(meta: dict, start: datetime, end: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    """A daily total's span, pinned to the calendar day the phone counted it on.

    The phone anchors its totals at its own local midnight and says which day it
    meant. Using that day keeps the total on the right date even if --timezone
    differs from the phone's zone; converting the UTC instant alone would not.
    """
    day = meta.get('calendar_day')
    if isinstance(day, str):
        try:
            local_start = datetime.combine(date.fromisoformat(day), time(0), tz)
            return local_start, local_start + (end - start)
        except ValueError:
            pass
    return start.astimezone(tz), end.astimezone(tz)


def _load_samples(conn, data: ExportData, tz: tzinfo, aliases: dict[str, str],
                  summary: BridgeSummary) -> None:
    query = (
        'select s.type_code, s.start_time, s.end_time, s.value, s.unit, s.metadata_json, '
        'src.name, s.created_at from samples s join sources src on src.source_id = s.source_id '
    )

    # Pass 1: which (type, local day) pairs have the phone's own daily total.
    with_daily_total: set[tuple[str, date]] = set()
    for type_code, start_raw, end_raw, _v, _u, meta_raw, _n, _c in conn.execute(
            query + "where s.metadata_json like '%daily_sum%' or s.metadata_json like '%HKStatisticsCollectionQuery%'"):
        meta = _metadata(meta_raw)
        if not is_daily_total(meta):
            continue
        hk = meta.get('healthkit_identifier') or aliases.get(type_code)
        start, _end = _local_day_span(meta, parse_utc(start_raw), parse_utc(end_raw), tz)
        with_daily_total.add((hk, start.date()))

    # Pass 2: everything, in time order, through the shared ingest.
    for type_code, start_raw, end_raw, value, unit, meta_raw, source, created in conn.execute(
            query + 'order by s.start_time'):
        meta = _metadata(meta_raw)
        hk = meta.get('healthkit_identifier') or aliases.get(type_code)
        if not hk:
            summary.unmapped_types[type_code] += 1
            continue
        data.type_counts[hk] += 1
        if hk not in ALL_PARSED_TYPES:
            summary.not_parsed_types[hk] += 1
            continue

        start, end = parse_utc(start_raw), parse_utc(end_raw)
        daily = is_daily_total(meta)
        if daily:
            start, end = _local_day_span(meta, start, end, tz)
        else:
            start, end = start.astimezone(tz), end.astimezone(tz)
            if (hk, start.date()) in with_daily_total:
                summary.raw_replaced_by_daily_total += 1
                continue

        if hk == STAND_HOUR_TYPE:
            label = stand_hour_label(float(value))
            if label is None:
                summary.unmapped_types[f'{type_code} value {value}'] += 1
                continue
            value_raw, export_unit = label, ''
        else:
            number, export_unit = to_export_value(hk, float(value), unit)
            value_raw = repr(number)
        name = DAILY_TOTAL_SOURCE if daily else (meta.get('healthkit_source_name') or source)
        created_at = _stamp(parse_utc(created).astimezone(tz)) if created else ''
        if add_parsed_record(
            data, hk,
            start_raw=_stamp(start), end_raw=_stamp(end),
            value_raw=value_raw, unit=export_unit,
            source_name=str(name), creation=created_at,
            start_dt=start, end_dt=end,
        ):
            summary.samples += 1
            if daily:
                summary.daily_totals += 1


def _load_sleep(conn, data: ExportData, tz: tzinfo, summary: BridgeSummary) -> None:
    stages: dict[int, list[tuple[str, str, str]]] = {}
    for session_id, stage, start_raw, end_raw in conn.execute(
            'select sleep_session_id, stage, start_time, end_time from sleep_stage_intervals'):
        stages.setdefault(session_id, []).append((stage, start_raw, end_raw))

    for session_id, start_raw, end_raw, source, created in conn.execute(
            'select ss.sleep_session_id, ss.start_time, ss.end_time, src.name, ss.created_at '
            'from sleep_sessions ss join sources src on src.source_id = ss.source_id '
            'order by ss.start_time'):
        # A session that arrives without stages still says the person was in
        # bed; reporting it as asleep would invent sleep that was not measured.
        intervals = stages.get(session_id) or [('in_bed', start_raw, end_raw)]
        created_at = _stamp(parse_utc(created).astimezone(tz)) if created else ''
        for stage, s_raw, e_raw in intervals:
            label = STAGE_TO_EXPORT.get(stage)
            if not label:
                summary.unmapped_types[f'sleep stage {stage}'] += 1
                continue
            start, end = parse_utc(s_raw).astimezone(tz), parse_utc(e_raw).astimezone(tz)
            data.type_counts[SLEEP_TYPE] += 1
            if add_parsed_record(
                data, SLEEP_TYPE,
                start_raw=_stamp(start), end_raw=_stamp(end),
                value_raw=label, unit='', source_name=source, creation=created_at,
                start_dt=start, end_dt=end,
            ):
                summary.sleep_stages += 1


def _load_workouts(conn, data: ExportData, tz: tzinfo, summary: BridgeSummary) -> None:
    for kind, start_raw, end_raw, seconds, kcal, meters, source, created in conn.execute(
            'select w.workout_type, w.start_time, w.end_time, w.duration_seconds, w.energy_kcal, '
            'w.distance_meters, src.name, w.created_at '
            'from workouts w join sources src on src.source_id = w.source_id order by w.start_time'):
        start, end = parse_utc(start_raw).astimezone(tz), parse_utc(end_raw).astimezone(tz)
        activity = WORKOUT_TO_EXPORT.get(kind, 'HKWorkoutActivityTypeOther')
        statistics: dict[str, dict] = {}
        if kcal is not None:
            statistics['HKQuantityTypeIdentifierActiveEnergyBurned'] = {
                'average': None, 'minimum': None, 'maximum': None, 'sum': float(kcal), 'unit': 'kcal'}
        if meters is not None and kind in WORKOUT_DISTANCE_STAT:
            statistics[WORKOUT_DISTANCE_STAT[kind]] = {
                'average': None, 'minimum': None, 'maximum': None,
                'sum': float(meters) / 1000.0, 'unit': 'km'}
        data.type_counts[f'Workout:{activity}'] += 1
        data.workouts.append(Workout(
            workout_activity_type=activity,
            start=start,
            end=end,
            start_raw=_stamp(start),
            end_raw=_stamp(end),
            creation=_stamp(parse_utc(created).astimezone(tz)) if created else '',
            duration=float(seconds) / 60.0 if seconds is not None else None,
            duration_unit='min',
            total_energy=float(kcal) if kcal is not None else None,
            total_energy_unit='kcal',
            total_distance=float(meters) if meters is not None else None,
            total_distance_unit='m',
            source_name=source,
            statistics=statistics,
        ))
        summary.workouts += 1
