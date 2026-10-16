#!/usr/bin/env python3
"""Turn an Apple Health export into a Health Bridge receiver database.

For testing health_bridge_import.py without an iPhone. The rows are shaped the
way the Health Bridge companion app sends them -- not the way export.xml stores
them -- because the differences are the whole point of the importer:

* timestamps in UTC with a trailing Z;
* summed metrics (steps, energy, distance, flights, stand time) as the phone's
  own per-day total, de-duplicated across devices, plus -- for steps -- the raw
  Watch samples as well, so the "a daily total beats raw samples" rule is
  exercised;
* percentages as 0-100, heart and breathing rates as bpm/brpm, distance in
  metres, temperature in degrees C written "°C", minutes as "minutes";
* sleep as sessions holding stage intervals, attributed to the phone app;
* the original device kept in `healthkit_source_name`, as the app does.

Types the companion app does not sync (stand-hour categories, cycling power)
are left out, so a comparison against export.xml also shows what an export
carries that the bridge does not.

By default the database gets a minimal schema covering only what the importer
reads. `--schema-dir` applies a real receiver's SQL migrations instead, which
is how to check the importer against the schema actually shipped.

    python3 fixtures/make_bridge_db.py --export demo/export.xml --out demo/bridge.sqlite
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health_coaching import (  # noqa: E402
    SLEEP_AWAKE, SLEEP_CORE, SLEEP_DEEP, SLEEP_IN_BED, SLEEP_REM, SLEEP_TYPE,
    ExportData, dedup_interval_sum, iter_export_xml, local_calendar_date,
)

MINIMAL_SCHEMA = """
create table sources (
    source_id integer primary key, source_key text not null unique, name text not null,
    kind text not null, bundle_id text, device_model text);
create table health_types (
    type_code text primary key, display_name text not null, category text not null,
    default_unit text not null, sensitivity text not null);
create table health_type_aliases (
    alias_id integer primary key, type_code text not null, alias text not null);
create table samples (
    sample_id integer primary key, source_id integer not null, type_code text not null,
    client_record_id text not null, start_time text not null, end_time text not null,
    value real not null, unit text not null, metadata_json text not null,
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    unique (source_id, type_code, client_record_id));
create table workouts (
    workout_id integer primary key, source_id integer not null, client_record_id text not null,
    workout_type text not null, start_time text not null, end_time text not null,
    duration_seconds integer not null, energy_kcal real, distance_meters real,
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    unique (source_id, client_record_id));
create table sleep_sessions (
    sleep_session_id integer primary key, source_id integer not null,
    client_record_id text not null, start_time text not null, end_time text not null,
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    unique (source_id, client_record_id));
create table sleep_stage_intervals (
    sleep_stage_interval_id integer primary key, sleep_session_id integer not null,
    stage text not null, start_time text not null, end_time text not null);
"""

Q = 'HKQuantityTypeIdentifier'

# HealthKit identifier -> (bridge type code, bridge unit, category, aggregation).
TYPES = {
    Q + 'StepCount': ('steps', 'count', 'activity', 'sum'),
    Q + 'ActiveEnergyBurned': ('active_energy', 'kcal', 'activity', 'sum'),
    Q + 'BasalEnergyBurned': ('basal_energy', 'kcal', 'activity', 'sum'),
    Q + 'DistanceWalkingRunning': ('distance_walking_running', 'm', 'activity', 'sum'),
    Q + 'FlightsClimbed': ('flights_climbed', 'count', 'activity', 'sum'),
    Q + 'AppleStandTime': ('stand_time', 'minutes', 'activity', 'sum'),
    Q + 'AppleExerciseTime': ('exercise_time', 'minutes', 'activity', 'sum'),
    Q + 'HeartRate': ('heart_rate', 'bpm', 'vitals', 'minMaxAverage'),
    Q + 'RestingHeartRate': ('resting_heart_rate', 'bpm', 'vitals', 'minMaxAverage'),
    Q + 'HeartRateVariabilitySDNN': ('heart_rate_variability_sdnn', 'ms', 'vitals', 'minMaxAverage'),
    Q + 'RespiratoryRate': ('respiratory_rate', 'brpm', 'vitals', 'minMaxAverage'),
    Q + 'OxygenSaturation': ('oxygen_saturation', '%', 'vitals', 'minMaxAverage'),
    Q + 'AppleSleepingWristTemperature': ('skin_temperature', '°C', 'vitals', 'minMaxAverage'),
    Q + 'BodyMass': ('body_mass', 'kg', 'body', 'latest'),
    Q + 'BodyFatPercentage': ('body_fat_percentage', '%', 'body', 'latest'),
    Q + 'VO2Max': ('vo2_max', 'mL/kg/min', 'fitness', 'latest'),
}
DAILY_TOTAL_TYPES = {hk for hk, (_c, _u, _cat, agg) in TYPES.items() if agg == 'sum'}
# Raw samples are also sent for these, alongside the daily total.
ALSO_RAW = {Q + 'StepCount'}

STAGES = {SLEEP_IN_BED: 'in_bed', SLEEP_AWAKE: 'awake', SLEEP_CORE: 'core',
          SLEEP_DEEP: 'deep', SLEEP_REM: 'rem'}
WORKOUTS = {
    'HKWorkoutActivityTypeRunning': 'running', 'HKWorkoutActivityTypeWalking': 'walking',
    'HKWorkoutActivityTypeCycling': 'cycling', 'HKWorkoutActivityTypeSwimming': 'swimming',
    'HKWorkoutActivityTypeHiking': 'hiking', 'HKWorkoutActivityTypeYoga': 'yoga',
}


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def to_bridge_value(hk: str, value: float, unit: str) -> float:
    """export.xml's number -> the number the companion app would have sent."""
    bridge_unit = TYPES[hk][1]
    if bridge_unit == '%':
        return value * 100.0
    if bridge_unit == 'm' and unit.lower() == 'km':
        return value * 1000.0
    return value


def create_schema(conn: sqlite3.Connection, schema_dir: str | None) -> None:
    if not schema_dir:
        conn.executescript(MINIMAL_SCHEMA)
        return
    for path in sorted(glob.glob(os.path.join(schema_dir, '*.sql'))):
        with open(path, encoding='utf-8') as handle:
            conn.executescript(handle.read())


def build(export_xml: str, out_path: str, tz_name: str, schema_dir: str | None = None) -> dict[str, int]:
    data = ExportData()
    iter_export_xml(export_xml, data, show_progress=False)
    raw = list(_all_raw(export_xml))   # read once; ~40k rows for the demo

    if os.path.exists(out_path):
        os.remove(out_path)
    conn = sqlite3.connect(out_path)
    create_schema(conn, schema_dir)
    conn.execute("insert into sources (source_key, name, kind, bundle_id, device_model) "
                 "values ('apple_health.phone', 'Apple Health on iPhone', 'phone', "
                 "'com.example.HealthBridgeCompanion', 'iPhone')")
    source_id = conn.execute('select source_id from sources').fetchone()[0]
    for hk, (code, unit, category, _agg) in TYPES.items():
        conn.execute('insert or ignore into health_types (type_code, display_name, category, '
                     'default_unit, sensitivity) values (?, ?, ?, ?, ?)',
                     (code, code.replace('_', ' ').title(), category, unit, 'moderate'))
        conn.execute('insert into health_type_aliases (type_code, alias) values (?, ?)', (code, hk))

    counts = defaultdict(int)
    serial = 0

    def sample(hk, start_text, end_text, value, metadata):
        nonlocal serial
        serial += 1
        code, unit, _cat, _agg = TYPES[hk]
        conn.execute('insert into samples (source_id, type_code, client_record_id, start_time, '
                     'end_time, value, unit, metadata_json) values (?, ?, ?, ?, ?, ?, ?, ?)',
                     (source_id, code, f'fixture-{serial}', start_text, end_text, value, unit,
                      json.dumps(metadata, sort_keys=True)))

    # Daily totals, the way HKStatisticsCollectionQuery hands them to the app:
    # one number per local day, already merged across devices.
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    for hk in sorted(DAILY_TOTAL_TYPES):
        if any(rec.type == hk for rec in data.records):
            per_day = dedup_interval_sum(data.records, hk, lambda value, _unit: value)
            unit_of = {rec.type: rec.unit for rec in data.records if rec.type == hk}
        else:
            per_day = {}
            unit_of = {}
        if not per_day:
            # Not retained for interval de-duplication (stand time, basal energy):
            # the plain per-day sum is what the daily accumulator would see.
            per_day = defaultdict(float)
            for start, _end, value, unit, record_type, _source in raw:
                if record_type == hk:
                    per_day[local_calendar_date(start)] += value
                    unit_of[hk] = unit
        for day, total in sorted(per_day.items()):
            if total <= 0:
                continue
            day_start = datetime.combine(day, time(0), tz)
            day_end = datetime.combine(day + timedelta(days=1), time(0), tz)
            sample(hk, utc(day_start), utc(day_end),
                   to_bridge_value(hk, total, unit_of.get(hk, '')),
                   {'aggregation': 'daily_sum', 'aggregation_completeness': 'complete',
                    'calendar_day': day.isoformat(), 'healthkit_identifier': hk,
                    'healthkit_query': 'HKStatisticsCollectionQuery',
                    'sample_kind': 'daily_aggregate', 'time_zone_identifier': tz_name})
            counts['daily_totals'] += 1

    # Raw samples: everything not summed, plus steps again as raw Watch samples.
    for start, end, value, unit, hk, source_name in raw:
        if hk not in TYPES:
            counts['skipped_unsupported'] += 1
            continue
        if hk in DAILY_TOTAL_TYPES and hk not in ALSO_RAW:
            continue
        sample(hk, utc(start), utc(end), to_bridge_value(hk, value, unit),
               {'aggregation': TYPES[hk][3], 'healthkit_identifier': hk,
                'healthkit_object_kind': 'quantity', 'healthkit_query': 'HKAnchoredObjectQuery',
                'healthkit_source_name': source_name, 'sample_kind': 'raw_quantity'})
        counts['raw_samples'] += 1

    # Sleep: sessions of contiguous stage records from one device.
    by_source = defaultdict(list)
    for rec in data.records:
        if rec.type == SLEEP_TYPE and rec.category_value in STAGES:
            by_source[rec.source_name].append(rec)
    for source_name, recs in by_source.items():
        recs.sort(key=lambda r: r.start)
        sessions, current = [], []
        for rec in recs:
            if current and rec.start - max(r.end for r in current) > timedelta(hours=2):
                sessions.append(current)
                current = []
            current.append(rec)
        if current:
            sessions.append(current)
        for index, session in enumerate(sessions):
            start, end = min(r.start for r in session), max(r.end for r in session)
            cur = conn.execute('insert into sleep_sessions (source_id, client_record_id, start_time, '
                               'end_time) values (?, ?, ?, ?)',
                               (source_id, f'fixture-sleep-{source_name}-{index}', utc(start), utc(end)))
            for rec in session:
                conn.execute('insert into sleep_stage_intervals (sleep_session_id, stage, start_time, '
                             'end_time) values (?, ?, ?, ?)',
                             (cur.lastrowid, STAGES[rec.category_value], utc(rec.start), utc(rec.end)))
                counts['sleep_stages'] += 1

    for index, workout in enumerate(data.workouts):
        minutes = workout.duration if (workout.duration_unit or 'min').lower().startswith('min') \
            else (workout.end - workout.start).total_seconds() / 60.0
        meters = None
        if workout.total_distance is not None:
            meters = workout.total_distance * (1000.0 if workout.total_distance_unit.lower() == 'km' else 1.0)
        conn.execute('insert into workouts (source_id, client_record_id, workout_type, start_time, '
                     'end_time, duration_seconds, energy_kcal, distance_meters) '
                     'values (?, ?, ?, ?, ?, ?, ?, ?)',
                     (source_id, f'fixture-workout-{index}',
                      WORKOUTS.get(workout.workout_activity_type, 'other'),
                      utc(workout.start), utc(workout.end), int(round((minutes or 0) * 60)),
                      workout.total_energy, meters))
        counts['workouts'] += 1

    conn.commit()
    conn.close()
    return dict(counts)


def _all_raw(export_xml: str):
    """Every Record with a numeric value, straight from the XML."""
    import xml.etree.ElementTree as ET

    from health_coaching import parse_float, parse_health_datetime
    for _event, elem in ET.iterparse(export_xml, events=('end',)):
        if elem.tag == 'Record':
            value = parse_float(elem.get('value', ''))
            start = parse_health_datetime(elem.get('startDate', ''))
            end = parse_health_datetime(elem.get('endDate', ''))
            if value is not None and start and end:
                yield start, end, value, elem.get('unit', '') or '', elem.get('type', ''), \
                    elem.get('sourceName', '') or ''
            elem.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--export', required=True, help='path to export.xml')
    parser.add_argument('--out', required=True, help='SQLite file to write (replaced if it exists)')
    parser.add_argument('--timezone', default='Etc/GMT-3',
                        help='IANA zone the phone counts days in. Default Etc/GMT-3, the fixed '
                             '+03:00 the synthetic demo export is written in.')
    parser.add_argument('--schema-dir', default=None,
                        help="a receiver's storage/migrations directory, to use its real schema")
    args = parser.parse_args()
    counts = build(args.export, args.out, args.timezone, args.schema_dir)
    print(f'Wrote {args.out}: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))


if __name__ == '__main__':
    main()
