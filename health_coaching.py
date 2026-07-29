"""Parse Apple Health exports and build coaching-ready daily/workout/weekly outputs."""

from __future__ import annotations

import csv
import hashlib
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Core metrics (legacy full_health_data.csv)
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

# Extra types for coaching aggregates (joined when present in export)
COACHING_EXTRA_TYPES = {
    'HKQuantityTypeIdentifierBasalEnergyBurned',
    'HKQuantityTypeIdentifierDistanceCycling',
    'HKQuantityTypeIdentifierCyclingPower',
    'HKQuantityTypeIdentifierCyclingCadence',
    'HKQuantityTypeIdentifierDistanceWalkingRunning',
}

COACHING_RECORD_TYPES = TARGET_TYPES | COACHING_EXTRA_TYPES

WORKOUT_ENRICHMENT_TYPES = {
    'HKQuantityTypeIdentifierHeartRate',
    'HKQuantityTypeIdentifierCyclingPower',
    'HKQuantityTypeIdentifierCyclingCadence',
}

SLEEP_IN_BED = 'HKCategoryValueSleepAnalysisInBed'
SLEEP_ASLEEP = 'HKCategoryValueSleepAnalysisAsleep'
SLEEP_ASLEEP_UNSPEC = 'HKCategoryValueSleepAnalysisAsleepUnspecified'
SLEEP_REM = 'HKCategoryValueSleepAnalysisAsleepREM'
SLEEP_CORE = 'HKCategoryValueSleepAnalysisAsleepCore'
SLEEP_DEEP = 'HKCategoryValueSleepAnalysisAsleepDeep'

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


@dataclass
class HealthRecord:
    type: str
    value: Optional[float]
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
    duration: Optional[float]
    duration_unit: str
    total_energy: Optional[float]
    total_energy_unit: str
    total_distance: Optional[float]
    total_distance_unit: str
    source_name: str
    statistics: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExportData:
    type_counts: Counter = field(default_factory=Counter)
    records: List[HealthRecord] = field(default_factory=list)
    workouts: List[Workout] = field(default_factory=list)
    seen_record_keys: Set[Tuple] = field(default_factory=set)
    timezone_label: str = ''


def parse_health_datetime(raw: str) -> Optional[datetime]:
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
    if dt.tzinfo:
        return dt.astimezone().date()
    return dt.date()


def parse_float(value: str) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def energy_to_kcal(amount: Optional[float], unit: str) -> Optional[float]:
    if amount is None:
        return None
    u = (unit or '').lower()
    if u in ('kcal', 'cal', ''):
        return amount
    if u == 'kj':
        return amount / 4.184
    return amount


def distance_to_km(amount: Optional[float], unit: str) -> Optional[float]:
    if amount is None:
        return None
    u = (unit or '').lower()
    if u in ('km', 'kilometer', 'kilometers'):
        return amount
    if u in ('m', 'meter', 'meters'):
        return amount / 1000.0
    if u in ('mi', 'mile', 'miles'):
        return amount * 1.60934
    return amount


def duration_to_minutes(amount: Optional[float], unit: str) -> Optional[float]:
    if amount is None:
        return None
    u = (unit or 'min').lower()
    if u in ('min', 'minute', 'minutes'):
        return amount
    if u in ('s', 'sec', 'second', 'seconds'):
        return amount / 60.0
    if u in ('h', 'hr', 'hour', 'hours'):
        return amount * 60.0
    return amount


def body_mass_to_kg(amount: Optional[float], unit: str) -> Optional[float]:
    if amount is None:
        return None
    u = (unit or 'kg').lower()
    if u in ('kg', 'kilogram', 'kilograms'):
        return amount
    if u in ('lb', 'lbs', 'pound', 'pounds'):
        return amount * 0.45359237
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


def infer_timezone_label(records: List[HealthRecord], workouts: List[Workout]) -> str:
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


def iter_export_xml(filepath: str, data: ExportData) -> int:
    import xml.etree.ElementTree as ET

    print(f"Processing: {filepath}")
    added = 0

    context = ET.iterparse(filepath, events=('end',))
    for _event, elem in context:
        if elem.tag == 'Record':
            record_type = elem.get('type') or ''
            data.type_counts[record_type] += 1

            if record_type in COACHING_RECORD_TYPES:
                start_raw = elem.get('startDate', '')
                end_raw = elem.get('endDate', '')
                value_raw = elem.get('value', '') or ''
                unit = elem.get('unit', '') or ''
                source = elem.get('sourceName', '') or ''
                creation = elem.get('creationDate', '') or ''

                key = (creation, start_raw, end_raw, record_type, value_raw, source)
                if key not in data.seen_record_keys:
                    data.seen_record_keys.add(key)
                    start_dt = parse_health_datetime(start_raw)
                    end_dt = parse_health_datetime(end_raw)
                    if start_dt and end_dt:
                        numeric = parse_float(value_raw)
                        data.records.append(
                            HealthRecord(
                                type=record_type,
                                value=numeric,
                                category_value=value_raw if numeric is None else '',
                                unit=unit,
                                source_name=source,
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

            stats: Dict[str, Dict[str, Any]] = {}
            metadata: Dict[str, str] = {}
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

    print(f"  Loaded {len(data.records):,} metric records, {len(data.workouts):,} workouts")
    return added


def sleep_segment_hours(start: datetime, end: datetime) -> float:
    seconds = (end - start).total_seconds()
    return max(seconds, 0) / 3600.0


def assign_sleep_to_day(start: datetime, end: datetime) -> date:
    """Attribute sleep to the wake calendar day (end date)."""
    return local_calendar_date(end)


def aggregate_sleep_by_day(records: List[HealthRecord]) -> Dict[date, Dict[str, float]]:
    per_day: Dict[date, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    seen_segments: Set[Tuple] = set()

    for rec in records:
        if rec.type != 'HKCategoryTypeIdentifierSleepAnalysis':
            continue
        label = rec.category_value or ''
        if not label:
            continue
        seg_key = (rec.start_raw, rec.end_raw, label)
        if seg_key in seen_segments:
            continue
        seen_segments.add(seg_key)

        day = assign_sleep_to_day(rec.start, rec.end)
        hours = sleep_segment_hours(rec.start, rec.end)
        if label == SLEEP_IN_BED:
            per_day[day]['in_bed'] += hours
        elif label in (SLEEP_ASLEEP, SLEEP_ASLEEP_UNSPEC):
            per_day[day]['asleep'] += hours
        elif label == SLEEP_REM:
            per_day[day]['rem'] += hours
        elif label == SLEEP_CORE:
            per_day[day]['core'] += hours
        elif label == SLEEP_DEEP:
            per_day[day]['deep'] += hours

    return per_day


def is_stand_hour_met(rec: HealthRecord) -> bool:
    label = (rec.category_value or '').lower()
    if label:
        # "applestandhour" is a substring of both the Stood and Idle category
        # values, so it can't be used to tell them apart — only "idle" and
        # "stood" themselves can. Pre-iOS-13-era exports predate the Idle
        # case entirely and use the bare value with no Stood/Idle suffix at
        # all; every record in that format is inherently a met hour, since
        # idle hours weren't recorded as samples yet.
        if 'idle' in label:
            return False
        return True
    if rec.value is not None and rec.value >= 1:
        return True
    return False


def build_daily_metrics(data: ExportData) -> Tuple[List[Dict[str, Any]], Set[str], Set[str]]:
    used_types: Set[str] = set()
    missing_types: Set[str] = set()


    by_day: Dict[date, Dict[str, Any]] = defaultdict(dict)
    sleep_by_day = aggregate_sleep_by_day(data.records)

    quantity_sum_types = {
        'HKQuantityTypeIdentifierStepCount': 'steps_total',
        'HKQuantityTypeIdentifierActiveEnergyBurned': 'active_kcal_total',
        'HKQuantityTypeIdentifierBasalEnergyBurned': 'basal_kcal_total',
    }
    quantity_avg_types = {
        'HKQuantityTypeIdentifierRestingHeartRate': 'resting_hr_avg',
        'HKQuantityTypeIdentifierHeartRateVariabilitySDNN': 'hrv_sdnn_avg',
        'HKQuantityTypeIdentifierRespiratoryRate': 'respiratory_rate_avg',
    }
    latest_types = {
        'HKQuantityTypeIdentifierBodyMass': ('body_mass_kg', body_mass_to_kg),
        'HKQuantityTypeIdentifierBodyFatPercentage': ('body_fat_pct', lambda v, u: v),
        'HKQuantityTypeIdentifierVO2Max': ('vo2max', lambda v, u: v),
    }

    sums: Dict[date, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    latest: Dict[date, Dict[str, Tuple[datetime, float]]] = defaultdict(dict)
    stand_counts: Dict[date, int] = defaultdict(int)

    for rec in data.records:
        day = local_calendar_date(rec.start)
        if rec.type in quantity_sum_types and rec.value is not None:
            col = quantity_sum_types[rec.type]
            kcal = energy_to_kcal(rec.value, rec.unit) if 'Energy' in rec.type else rec.value
            sums[day][col].append(kcal)
            used_types.add(rec.type)

        if rec.type in quantity_avg_types and rec.value is not None:
            sums[day][quantity_avg_types[rec.type]].append(rec.value)
            used_types.add(rec.type)

        if rec.type in latest_types and rec.value is not None:
            col, conv = latest_types[rec.type]
            val = conv(rec.value, rec.unit)
            prev = latest[day].get(col)
            if prev is None or rec.end > prev[0]:
                latest[day][col] = (rec.end, val)
            used_types.add(rec.type)

        if rec.type == 'HKCategoryTypeIdentifierAppleStandHour' and is_stand_hour_met(rec):
            stand_counts[day] += 1
            used_types.add(rec.type)

    all_days: Set[date] = set(sums.keys()) | set(sleep_by_day.keys()) | set(latest.keys()) | set(stand_counts.keys())

    rows: List[Dict[str, Any]] = []
    for day in sorted(all_days):
        row: Dict[str, Any] = {'date': day.isoformat()}

        for col in ('steps_total', 'active_kcal_total', 'basal_kcal_total'):
            vals = sums[day].get(col, [])
            row[col] = round(sum(vals), 2) if vals else ''

        for col in ('resting_hr_avg', 'hrv_sdnn_avg', 'respiratory_rate_avg'):
            vals = sums[day].get(col, [])
            row[col] = round(statistics.mean(vals), 2) if vals else ''

        sleep = sleep_by_day.get(day, {})
        row['sleep_in_bed_hours'] = round(sleep.get('in_bed', 0), 3) if sleep.get('in_bed') else ''
        asleep = sleep.get('asleep', 0)
        stage_sum = sleep.get('rem', 0) + sleep.get('core', 0) + sleep.get('deep', 0)
        if asleep:
            row['sleep_asleep_hours'] = round(asleep, 3)
        elif stage_sum:
            row['sleep_asleep_hours'] = round(stage_sum, 3)
        else:
            row['sleep_asleep_hours'] = ''
        row['sleep_rem_hours'] = round(sleep['rem'], 3) if sleep.get('rem') else ''
        row['sleep_core_hours'] = round(sleep['core'], 3) if sleep.get('core') else ''
        row['sleep_deep_hours'] = round(sleep['deep'], 3) if sleep.get('deep') else ''

        for col in ('body_mass_kg', 'body_fat_pct', 'vo2max'):
            if col in latest[day]:
                row[col] = round(latest[day][col][1], 3)
            else:
                row[col] = ''

        row['stand_hours_met_count'] = stand_counts[day] if stand_counts[day] else ''

        rows.append(row)

    for t in COACHING_RECORD_TYPES:
        if data.type_counts.get(t, 0) > 0:
            used_types.add(t)
        else:
            missing_types.add(t)

    return rows, used_types, missing_types


def hr_zone_for_bpm(bpm: float, max_hr: float) -> str:
    pct = bpm / max_hr
    for bound, zone in HR_ZONE_BOUNDS:
        if pct < bound:
            return zone
    return 'z5'


def heart_rate_samples(data: ExportData) -> List[HealthRecord]:
    return [r for r in data.records if r.type == 'HKQuantityTypeIdentifierHeartRate' and r.value is not None]


def cycling_metric_samples(data: ExportData, metric_type: str) -> List[HealthRecord]:
    return [r for r in data.records if r.type == metric_type and r.value is not None]


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
    hr_samples: List[HealthRecord],
    max_hr: float = DEFAULT_MAX_HR,
) -> Dict[str, Any]:
    in_window = [
        s
        for s in hr_samples
        if s.start < workout.end and s.end > workout.start
    ]
    in_window.sort(key=lambda s: s.start)

    zone_mins = {f'hr_zone_{z}_min': 0.0 for _, z in HR_ZONE_BOUNDS}
    bpm_values: List[float] = []

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

    result: Dict[str, Any] = {
        'avg_heart_rate_bpm': round(statistics.mean(bpm_values), 1) if bpm_values else '',
        'max_heart_rate_bpm': round(max(bpm_values), 1) if bpm_values else '',
    }
    for key, val in zone_mins.items():
        result[key] = round(val, 2) if val > 0 else ''
    return result


def stat_value(workout: Workout, stat_type: str, field: str = 'average') -> Optional[float]:
    stat = workout.statistics.get(stat_type, {})
    return stat.get(field)


def metrics_in_workout_window(
    workout: Workout,
    samples: List[HealthRecord],
) -> List[float]:
    vals = []
    for s in samples:
        if s.start < workout.end and s.end > workout.start:
            if s.value is not None:
                vals.append(s.value)
    return vals


def build_workout_summary(data: ExportData) -> List[Dict[str, Any]]:
    hr_samples = heart_rate_samples(data)
    power_samples = cycling_metric_samples(data, 'HKQuantityTypeIdentifierCyclingPower')
    cadence_samples = cycling_metric_samples(data, 'HKQuantityTypeIdentifierCyclingCadence')

    rows: List[Dict[str, Any]] = []
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

        hr_stats = compute_workout_hr_zones(workout, hr_samples)
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

        row = {
            'workout_id': workout_id_for(workout),
            'activity_type': friendly_workout_type(workout.workout_activity_type),
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
            if power_vals
            else (round(power_avg, 1) if power_avg is not None else ''),
            'cycling_power_max_w': round(max(power_vals), 1)
            if power_vals
            else (round(power_max, 1) if power_max is not None else ''),
            'cycling_cadence_avg_rpm': round(statistics.mean(cadence_vals), 1)
            if cadence_vals
            else (round(cadence_avg, 1) if cadence_avg is not None else ''),
            'source_name': workout.source_name,
        }
        rows.append(row)

    return rows


def iso_week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f'{year}-W{week:02d}'


def build_weekly_summary(
    daily_rows: List[Dict[str, Any]],
    workouts: List[Workout],
    workout_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    weeks: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    for row in daily_rows:
        if not row.get('date'):
            continue
        d = date.fromisoformat(row['date'])
        wk = iso_week_key(d)
        bucket = weeks[wk]
        if row.get('steps_total') != '':
            bucket.setdefault('steps', []).append(float(row['steps_total']))
        asleep = row.get('sleep_asleep_hours')
        if asleep != '':
            bucket.setdefault('sleep', []).append(float(asleep))
        if row.get('resting_hr_avg') != '':
            bucket.setdefault('resting_hr', []).append(float(row['resting_hr_avg']))
        if row.get('hrv_sdnn_avg') != '':
            bucket.setdefault('hrv', []).append(float(row['hrv_sdnn_avg']))
        if row.get('body_mass_kg') != '':
            bucket.setdefault('weight', []).append(float(row['body_mass_kg']))
        if row.get('body_fat_pct') != '':
            bucket.setdefault('body_fat', []).append(float(row['body_fat_pct']))

    workout_weeks: Dict[str, List[Workout]] = defaultdict(list)
    for w in workouts:
        workout_weeks[iso_week_key(local_calendar_date(w.start))].append(w)

    workout_row_by_id = {r['workout_id']: r for r in workout_rows}

    all_weeks = sorted(set(weeks.keys()) | set(workout_weeks.keys()))

    rows: List[Dict[str, Any]] = []
    for wk in all_weeks:
        wlist = workout_weeks.get(wk, [])
        strength = sum(1 for w in wlist if w.workout_activity_type in STRENGTH_WORKOUT_TYPES)
        cycling = sum(1 for w in wlist if w.workout_activity_type in CYCLING_WORKOUT_TYPES)
        walking = sum(1 for w in wlist if w.workout_activity_type in WALKING_WORKOUT_TYPES)

        z2_total = 0.0
        for w in wlist:
            row = workout_row_by_id.get(workout_id_for(w), {})
            z2 = row.get('hr_zone_z2_min')
            if z2 != '':
                z2_total += float(z2)

        b = weeks.get(wk, {})
        rows.append(
            {
                'iso_week': wk,
                'workouts_total': len(wlist) if wlist else '',
                'strength_sessions': strength if wlist else '',
                'cycling_sessions': cycling if wlist else '',
                'walking_sessions': walking if wlist else '',
                'zone2_minutes_estimate': round(z2_total, 1) if z2_total else '',
                'total_steps': round(sum(b.get('steps', [])), 0) if b.get('steps') else '',
                'avg_sleep_hours': round(statistics.mean(b['sleep']), 2) if b.get('sleep') else '',
                'avg_resting_hr': round(statistics.mean(b['resting_hr']), 1) if b.get('resting_hr') else '',
                'avg_hrv_sdnn': round(statistics.mean(b['hrv']), 1) if b.get('hrv') else '',
                'weight_week_avg': round(statistics.mean(b['weight']), 2) if b.get('weight') else '',
                'body_fat_week_avg': round(statistics.mean(b['body_fat']), 2) if b.get('body_fat') else '',
            }
        )

    return rows


def write_csv(path: str, fieldnames: List[str], rows: Iterable[Dict[str, Any]]) -> None:
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
            used = (
                t in COACHING_RECORD_TYPES
                or t.startswith('Workout:')
                or t == 'ActivitySummary'
            )
            writer.writerow([t, count, 'yes' if used else 'no'])


def empty_ratio(rows: List[Dict[str, Any]], column: str) -> float:
    if not rows:
        return 1.0
    empty = sum(1 for r in rows if r.get(column) in ('', None))
    return empty / len(rows)


def detect_duplicate_bursts(records: List[HealthRecord], threshold: int = 50) -> List[str]:
    by_minute: Counter = Counter()
    for rec in records:
        key = (rec.type, rec.start.replace(second=0, microsecond=0))
        by_minute[key] += 1
    bursts = [(k, c) for k, c in by_minute.items() if c >= threshold]
    lines = []
    for (rtype, minute), count in sorted(bursts, key=lambda x: -x[1])[:10]:
        lines.append(f"  {rtype} @ {minute.isoformat()}: {count} records in one minute")
    return lines


def detect_day_gaps(daily_rows: List[Dict[str, Any]], max_gap_days: int = 3) -> List[str]:
    dates = sorted(date.fromisoformat(r['date']) for r in daily_rows if r.get('date'))
    if len(dates) < 2:
        return []
    gaps = []
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        if delta > max_gap_days:
            gaps.append(f"  {dates[i-1].isoformat()} -> {dates[i].isoformat()}: {delta} days without daily row")
    return gaps


def write_data_quality_report(
    path: str,
    daily_rows: List[Dict[str, Any]],
    workout_rows: List[Dict[str, Any]],
    weekly_rows: List[Dict[str, Any]],
    data: ExportData,
    used_types: Set[str],
    missing_types: Set[str],
    tz_label: str,
) -> None:
    daily_cols = [
        'steps_total', 'active_kcal_total', 'basal_kcal_total', 'resting_hr_avg', 'hrv_sdnn_avg',
        'sleep_in_bed_hours', 'sleep_asleep_hours', 'sleep_rem_hours', 'sleep_core_hours', 'sleep_deep_hours',
        'body_mass_kg', 'body_fat_pct', 'vo2max', 'respiratory_rate_avg', 'stand_hours_met_count',
    ]
    workout_cols = [
        'duration_min', 'total_energy_kcal', 'active_energy_kcal', 'distance_km',
        'avg_heart_rate_bpm', 'max_heart_rate_bpm', 'hr_zone_z2_min',
        'cycling_power_avg_w', 'cycling_cadence_avg_rpm',
    ]

    all_dates = [date.fromisoformat(r['date']) for r in daily_rows if r.get('date')]
    date_range = ''
    if all_dates:
        date_range = f"{min(all_dates).isoformat()} to {max(all_dates).isoformat()} ({len(all_dates)} days)"

    lines = [
        'Apple Health Coaching Data Quality Report',
        '=' * 50,
        f'Timezone handling: dates use each record\'s embedded offset; dominant offset sample: {tz_label}',
        f'Date range (daily_metrics): {date_range or "n/a"}',
        '',
        'Types used for coaching aggregates:',
    ]
    for t in sorted(used_types):
        lines.append(f'  [present] {t} ({data.type_counts.get(t, 0):,} in export)')
    lines.append('')
    lines.append('Workout enrichment types (HR / cycling metrics):')
    for t in sorted(WORKOUT_ENRICHMENT_TYPES):
        count = data.type_counts.get(t, 0)
        if count:
            lines.append(f'  [present] {t} ({count:,} in export)')
        else:
            lines.append(f'  [missing] {t}')
    lines.append('')
    lines.append('Expected coaching types not found in export:')
    if missing_types:
        for t in sorted(missing_types):
            lines.append(f'  [missing] {t}')
    else:
        lines.append('  (none — all expected types had at least one record)')

    lines.extend(['', 'Missingness — daily_metrics.csv (% empty):'])
    for col in daily_cols:
        pct = empty_ratio(daily_rows, col) * 100
        lines.append(f'  {col}: {pct:.1f}%')

    lines.extend(['', 'Missingness — workout_summary.csv (% empty):'])
    for col in workout_cols:
        pct = empty_ratio(workout_rows, col) * 100
        lines.append(f'  {col}: {pct:.1f}%')

    lines.extend(['', f'Outputs: {len(daily_rows)} daily rows, {len(workout_rows)} workouts, {len(weekly_rows)} weeks'])

    gap_lines = detect_day_gaps(daily_rows)
    lines.append('')
    lines.append('Suspicious day gaps (>3 days without a daily row):')
    lines.extend(gap_lines if gap_lines else ['  none detected'])

    burst_lines = detect_duplicate_bursts(data.records)
    lines.append('')
    lines.append('Duplicate bursts (>=50 identical type+minute buckets):')
    lines.extend(burst_lines if burst_lines else ['  none detected'])

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def records_to_legacy_rows(data: ExportData) -> List[Dict[str, str]]:
    """Backward-compatible flat rows for full_health_data.csv."""
    rows: List[Dict[str, str]] = []
    legacy_types = TARGET_TYPES

    for rec in data.records:
        if rec.type not in legacy_types:
            continue
        val = rec.category_value if rec.category_value else ('' if rec.value is None else str(rec.value))
        rows.append(
            {
                'creationDate': rec.creation,
                'startDate': rec.start_raw,
                'endDate': rec.end_raw,
                'type': rec.type,
                'value': val,
            }
        )

    for workout in data.workouts:
        value_parts = []
        if workout.duration is not None:
            value_parts.append(f"duration:{workout.duration} {workout.duration_unit}")
        if workout.total_energy is not None:
            value_parts.append(f"calories:{workout.total_energy} {workout.total_energy_unit}")
        value = '; '.join(value_parts)
        rows.append(
            {
                'creationDate': workout.creation,
                'startDate': workout.start_raw,
                'endDate': workout.end_raw,
                'type': workout.workout_activity_type,
                'value': value,
            }
        )

    rows.sort(key=lambda r: r['startDate'])
    return rows


def write_coaching_outputs(base_dir: str, data: ExportData) -> Dict[str, str]:
    data.timezone_label = infer_timezone_label(data.records, data.workouts)

    daily_rows, used_types, missing_types = build_daily_metrics(data)
    workout_rows = build_workout_summary(data)
    weekly_rows = build_weekly_summary(daily_rows, data.workouts, workout_rows)

    paths = {
        'daily_metrics': os.path.join(base_dir, 'daily_metrics.csv'),
        'workout_summary': os.path.join(base_dir, 'workout_summary.csv'),
        'weekly_summary': os.path.join(base_dir, 'weekly_summary.csv'),
        'available_types': os.path.join(base_dir, 'available_types.csv'),
        'data_quality_report': os.path.join(base_dir, 'data_quality_report.txt'),
        'coaching_types_usage': os.path.join(base_dir, 'coaching_types_usage.txt'),
    }

    write_csv(
        paths['daily_metrics'],
        [
            'date', 'steps_total', 'active_kcal_total', 'basal_kcal_total', 'resting_hr_avg', 'hrv_sdnn_avg',
            'sleep_in_bed_hours', 'sleep_asleep_hours', 'sleep_rem_hours', 'sleep_core_hours', 'sleep_deep_hours',
            'body_mass_kg', 'body_fat_pct', 'vo2max', 'respiratory_rate_avg', 'stand_hours_met_count',
        ],
        daily_rows,
    )
    write_csv(
        paths['workout_summary'],
        [
            'workout_id', 'activity_type', 'start_datetime', 'end_datetime', 'duration_min',
            'total_energy_kcal', 'active_energy_kcal', 'distance_km', 'avg_heart_rate_bpm', 'max_heart_rate_bpm',
            'hr_zone_z1_min', 'hr_zone_z2_min', 'hr_zone_z3_min', 'hr_zone_z4_min', 'hr_zone_z5_min',
            'cycling_power_avg_w', 'cycling_power_max_w', 'cycling_cadence_avg_rpm', 'source_name',
        ],
        workout_rows,
    )
    write_csv(
        paths['weekly_summary'],
        [
            'iso_week', 'workouts_total', 'strength_sessions', 'cycling_sessions', 'walking_sessions',
            'zone2_minutes_estimate', 'total_steps', 'avg_sleep_hours', 'avg_resting_hr', 'avg_hrv_sdnn',
            'weight_week_avg', 'body_fat_week_avg',
        ],
        weekly_rows,
    )
    write_available_types(paths['available_types'], data.type_counts)
    write_data_quality_report(
        paths['data_quality_report'],
        daily_rows,
        workout_rows,
        weekly_rows,
        data,
        used_types,
        missing_types,
        data.timezone_label,
    )

    with open(paths['coaching_types_usage'], 'w', encoding='utf-8') as f:
        f.write('Run-time coaching type usage (see data_quality_report.txt for full diagnostics)\n\n')
        f.write('Used:\n')
        for t in sorted(used_types):
            f.write(f'  {t}\n')
        f.write('\nMissing from export:\n')
        for t in sorted(missing_types):
            f.write(f'  {t}\n')

    return paths

