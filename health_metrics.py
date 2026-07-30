"""Declarative registry of Apple Health metrics and how to aggregate them daily.

Adding a new metric to the coaching outputs means adding one MetricSpec here —
parsing, unit conversion, daily aggregation, CSV columns, and the coverage
report all read from this table.

Unit note: HealthKit's `%` unit is a *fraction* (0.183 = 18.3%), so every
percent-valued metric carries scale=100.0 to land in human-readable percent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

# Aggregations supported by DailyAccumulator.
#   sum      — add every sample in the day (counts, energy, distance, minutes)
#   mean     — arithmetic mean of samples in the day
#   min/max  — extreme sample of the day
#   latest   — value of the sample with the latest end timestamp that day
AGGREGATIONS = {'sum', 'mean', 'min', 'max', 'latest'}


@dataclass(frozen=True)
class MetricSpec:
    hk_type: str
    column: str
    agg: str
    scale: float = 1.0
    unit_kind: str = ''      # 'energy' | 'distance' | 'mass' | '' (no conversion)
    group: str = 'other'
    label: str = ''
    round_to: int = 2

    def convert(self, value: float, unit: str) -> float:
        if self.unit_kind == 'energy':
            value = _energy_to_kcal(value, unit)
        elif self.unit_kind == 'distance':
            value = _distance_to_km(value, unit)
        elif self.unit_kind == 'mass':
            value = _mass_to_kg(value, unit)
        return value * self.scale


def _energy_to_kcal(amount: float, unit: str) -> float:
    u = (unit or '').lower()
    if u == 'kj':
        return amount / 4.184
    return amount  # kcal / Cal / blank are already kilocalories


def _distance_to_km(amount: float, unit: str) -> float:
    u = (unit or '').lower()
    if u in ('m', 'meter', 'meters'):
        return amount / 1000.0
    if u in ('mi', 'mile', 'miles'):
        return amount * 1.60934
    return amount


def _mass_to_kg(amount: float, unit: str) -> float:
    u = (unit or '').lower()
    if u in ('lb', 'lbs', 'pound', 'pounds'):
        return amount * 0.45359237
    if u in ('g', 'gram', 'grams'):
        return amount / 1000.0
    return amount


Q = 'HKQuantityTypeIdentifier'
C = 'HKCategoryTypeIdentifier'

# The registry. Order here is the order of columns in daily_metrics.csv.
DAILY_SPECS: List[MetricSpec] = [
    # --- Activity -----------------------------------------------------------
    MetricSpec(Q + 'StepCount', 'steps', 'sum', group='activity', label='Steps', round_to=0),
    MetricSpec(Q + 'DistanceWalkingRunning', 'distance_km', 'sum', unit_kind='distance',
               group='activity', label='Walk/run distance (km)', round_to=3),
    MetricSpec(Q + 'FlightsClimbed', 'flights_climbed', 'sum', group='activity',
               label='Flights climbed', round_to=0),
    MetricSpec(Q + 'ActiveEnergyBurned', 'active_kcal', 'sum', unit_kind='energy',
               group='activity', label='Active energy (kcal)', round_to=0),
    MetricSpec(Q + 'BasalEnergyBurned', 'basal_kcal', 'sum', unit_kind='energy',
               group='activity', label='Basal energy (kcal)', round_to=0),
    MetricSpec(Q + 'AppleExerciseTime', 'exercise_minutes', 'sum', group='activity',
               label='Exercise minutes', round_to=0),
    MetricSpec(Q + 'AppleStandTime', 'stand_minutes', 'sum', group='activity',
               label='Stand minutes', round_to=0),
    MetricSpec(Q + 'TimeInDaylight', 'daylight_minutes', 'sum', group='activity',
               label='Time in daylight (min)', round_to=0),
    MetricSpec(Q + 'DistanceCycling', 'cycling_km', 'sum', unit_kind='distance',
               group='activity', label='Cycling distance (km)', round_to=3),

    # --- Cardiovascular -----------------------------------------------------
    MetricSpec(Q + 'RestingHeartRate', 'resting_hr', 'mean', group='cardio',
               label='Resting heart rate (bpm)', round_to=1),
    MetricSpec(Q + 'WalkingHeartRateAverage', 'walking_hr', 'mean', group='cardio',
               label='Walking heart rate (bpm)', round_to=1),
    MetricSpec(Q + 'HeartRateVariabilitySDNN', 'hrv_sdnn', 'mean', group='cardio',
               label='HRV SDNN (ms)', round_to=1),
    MetricSpec(Q + 'HeartRateRecoveryOneMinute', 'hr_recovery_1min', 'max', group='cardio',
               label='1-min heart-rate recovery (bpm)', round_to=1),
    MetricSpec(Q + 'VO2Max', 'vo2max', 'latest', group='cardio',
               label='VO2 max (ml/kg/min)', round_to=1),
    MetricSpec(Q + 'AtrialFibrillationBurden', 'afib_burden_pct', 'mean', scale=100.0,
               group='cardio', label='AFib burden (%)', round_to=2),

    # --- Respiratory / overnight vitals -------------------------------------
    MetricSpec(Q + 'RespiratoryRate', 'respiratory_rate', 'mean', group='vitals',
               label='Respiratory rate (breaths/min)', round_to=2),
    MetricSpec(Q + 'OxygenSaturation', 'spo2_avg', 'mean', scale=100.0, group='vitals',
               label='Blood oxygen average (%)', round_to=2),
    MetricSpec(Q + 'OxygenSaturation', 'spo2_min', 'min', scale=100.0, group='vitals',
               label='Blood oxygen minimum (%)', round_to=2),
    MetricSpec(Q + 'AppleSleepingWristTemperature', 'wrist_temp_c', 'mean', group='vitals',
               label='Sleeping wrist temperature (degC)', round_to=3),
    MetricSpec(Q + 'AppleSleepingBreathingDisturbances', 'breathing_disturbances', 'mean',
               group='vitals', label='Sleeping breathing disturbances', round_to=3),

    # --- Body composition ---------------------------------------------------
    MetricSpec(Q + 'BodyMass', 'body_mass_kg', 'latest', unit_kind='mass', group='body',
               label='Body mass (kg)', round_to=2),
    MetricSpec(Q + 'BodyFatPercentage', 'body_fat_pct', 'latest', scale=100.0, group='body',
               label='Body fat (%)', round_to=2),
    MetricSpec(Q + 'LeanBodyMass', 'lean_body_mass_kg', 'latest', unit_kind='mass', group='body',
               label='Lean body mass (kg)', round_to=2),
    MetricSpec(Q + 'BodyMassIndex', 'bmi', 'latest', group='body', label='BMI', round_to=2),
    MetricSpec(Q + 'Height', 'height_cm', 'latest', group='body', label='Height (cm)', round_to=1),

    # --- Mobility / gait ----------------------------------------------------
    MetricSpec(Q + 'WalkingSpeed', 'walking_speed_kmh', 'mean', group='mobility',
               label='Walking speed (km/h)', round_to=3),
    MetricSpec(Q + 'WalkingStepLength', 'walking_step_length_cm', 'mean', group='mobility',
               label='Walking step length (cm)', round_to=1),
    MetricSpec(Q + 'WalkingAsymmetryPercentage', 'walking_asymmetry_pct', 'mean', scale=100.0,
               group='mobility', label='Walking asymmetry (%)', round_to=2),
    MetricSpec(Q + 'WalkingDoubleSupportPercentage', 'walking_double_support_pct', 'mean',
               scale=100.0, group='mobility', label='Double support (%)', round_to=2),
    MetricSpec(Q + 'AppleWalkingSteadiness', 'walking_steadiness_pct', 'latest', scale=100.0,
               group='mobility', label='Walking steadiness (%)', round_to=1),
    MetricSpec(Q + 'StairAscentSpeed', 'stair_ascent_speed_ms', 'mean', group='mobility',
               label='Stair ascent speed (m/s)', round_to=3),
    MetricSpec(Q + 'StairDescentSpeed', 'stair_descent_speed_ms', 'mean', group='mobility',
               label='Stair descent speed (m/s)', round_to=3),
    MetricSpec(Q + 'SixMinuteWalkTestDistance', 'six_min_walk_m', 'latest', group='mobility',
               label='Six-minute walk distance (m)', round_to=0),

    # --- Nutrition ----------------------------------------------------------
    MetricSpec(Q + 'DietaryEnergyConsumed', 'diet_kcal', 'sum', unit_kind='energy',
               group='nutrition', label='Calories eaten (kcal)', round_to=0),
    MetricSpec(Q + 'DietaryProtein', 'diet_protein_g', 'sum', group='nutrition',
               label='Protein (g)', round_to=1),
    MetricSpec(Q + 'DietaryCarbohydrates', 'diet_carbs_g', 'sum', group='nutrition',
               label='Carbohydrates (g)', round_to=1),
    MetricSpec(Q + 'DietaryFatTotal', 'diet_fat_g', 'sum', group='nutrition',
               label='Fat (g)', round_to=1),
    MetricSpec(Q + 'DietaryFiber', 'diet_fiber_g', 'sum', group='nutrition',
               label='Fiber (g)', round_to=1),
    MetricSpec(Q + 'DietarySugar', 'diet_sugar_g', 'sum', group='nutrition',
               label='Sugar (g)', round_to=1),
    MetricSpec(Q + 'DietarySodium', 'diet_sodium_mg', 'sum', group='nutrition',
               label='Sodium (mg)', round_to=0),
    MetricSpec(Q + 'DietaryWater', 'diet_water_ml', 'sum', group='nutrition',
               label='Water (mL)', round_to=0),
    MetricSpec(Q + 'NumberOfAlcoholicBeverages', 'alcohol_drinks', 'sum', group='nutrition',
               label='Alcoholic drinks', round_to=1),

    # --- Environment --------------------------------------------------------
    MetricSpec(Q + 'HeadphoneAudioExposure', 'headphone_audio_db', 'mean', group='environment',
               label='Headphone audio exposure (dBASPL)', round_to=1),
    MetricSpec(Q + 'EnvironmentalAudioExposure', 'environment_audio_db', 'mean',
               group='environment', label='Environmental audio (dBASPL)', round_to=1),
]

# Columns produced outside the registry (sleep, effort, wear, mindfulness).
DERIVED_DAILY_COLUMNS: List[Tuple[str, str, str]] = [
    ('effort_mets_avg', 'effort', 'Average physical effort (METs, time-weighted)'),
    ('effort_moderate_min', 'effort', 'Minutes at >=3 METs'),
    ('effort_vigorous_min', 'effort', 'Minutes at >=6 METs'),
    ('stand_hours_met', 'activity', 'Stand hours met'),
    ('mindful_minutes', 'mind', 'Mindful session minutes'),
    ('sleep_in_bed_hours', 'sleep', 'Time in bed (hours)'),
    ('sleep_asleep_hours', 'sleep', 'Total sleep (hours)'),
    ('sleep_rem_hours', 'sleep', 'REM sleep (hours)'),
    ('sleep_core_hours', 'sleep', 'Core/light sleep (hours)'),
    ('sleep_deep_hours', 'sleep', 'Deep sleep (hours)'),
    ('sleep_awake_hours', 'sleep', 'Awake during sleep window (hours)'),
    ('sleep_efficiency_pct', 'sleep', 'Asleep / in-bed (%)'),
    ('sleep_awakenings', 'sleep', 'Number of awake segments'),
    ('sleep_deep_pct', 'sleep', 'Deep as % of total sleep'),
    ('sleep_rem_pct', 'sleep', 'REM as % of total sleep'),
    ('sleep_onset', 'sleep', 'Clock time asleep (local HH:MM)'),
    ('sleep_wake', 'sleep', 'Clock time awake (local HH:MM)'),
    ('sleep_midpoint_hour', 'sleep', 'Sleep midpoint as decimal hour (circadian anchor)'),
    ('sleep_source', 'sleep', 'Device chosen as authoritative for the night'),
    ('wear_hours', 'quality', 'Distinct hours with a watch sample'),
    ('wear_class', 'quality', 'full / partial / minimal / none'),
]

# HeartRate/cycling samples must stay in memory for workout time-window joins;
# everything else in the registry is folded into daily buckets during parsing
# and never retained, which is what keeps a ~1 GB export inside a sane RSS.
WINDOW_JOIN_TYPES: Set[str] = {
    Q + 'HeartRate',
    Q + 'CyclingPower',
    Q + 'CyclingCadence',
    Q + 'CyclingSpeed',
}

SLEEP_TYPE = C + 'SleepAnalysis'
STAND_HOUR_TYPE = C + 'AppleStandHour'
MINDFUL_TYPE = C + 'MindfulSession'
EFFORT_TYPE = Q + 'PhysicalEffort'

# Samples that require the watch to actually be against skin.
#
# Deliberately excludes BasalEnergyBurned and ActiveEnergyBurned: basal energy
# is a model (BMR x elapsed time) the watch emits around the clock whether or
# not it is being worn, and active energy is partly motion-derived. Including
# them marks every single day as fully worn, which silently defeats the entire
# point of wear detection. Heart rate, physical effort and stand time all
# depend on the optical sensor seeing a wrist.
WEAR_SIGNAL_TYPES: Set[str] = {
    Q + 'HeartRate',
    Q + 'PhysicalEffort',
    Q + 'AppleStandTime',
}

SPECIAL_TYPES: Set[str] = {SLEEP_TYPE, STAND_HOUR_TYPE, MINDFUL_TYPE, EFFORT_TYPE}

REGISTRY_TYPES: Set[str] = {spec.hk_type for spec in DAILY_SPECS}

# Every type the parser cares about at all.
ALL_PARSED_TYPES: Set[str] = REGISTRY_TYPES | SPECIAL_TYPES | WINDOW_JOIN_TYPES | WEAR_SIGNAL_TYPES

SPECS_BY_TYPE: Dict[str, List[MetricSpec]] = {}
for _spec in DAILY_SPECS:
    SPECS_BY_TYPE.setdefault(_spec.hk_type, []).append(_spec)

REGISTRY_COLUMNS: List[str] = [spec.column for spec in DAILY_SPECS]

COLUMN_META: Dict[str, Tuple[str, str]] = {
    spec.column: (spec.group, spec.label or spec.column) for spec in DAILY_SPECS
}
for _col, _group, _label in DERIVED_DAILY_COLUMNS:
    COLUMN_META[_col] = (_group, _label)

# Body composition and fitness markers are measured occasionally, not daily.
# A blank day means "not measured", not "missing data", so the quality report
# must not treat them like a dropped continuous stream.
SPARSE_BY_DESIGN: Set[str] = {
    'body_mass_kg', 'body_fat_pct', 'lean_body_mass_kg', 'bmi', 'height_cm',
    'vo2max', 'hr_recovery_1min', 'six_min_walk_m', 'walking_steadiness_pct',
    'afib_burden_pct', 'alcohol_drinks', 'mindful_minutes',
}

# Columns that should be carried forward when interpolating a "current value"
# (a weight measured Monday is still your best estimate of Tuesday's weight).
CARRY_FORWARD: Set[str] = {
    'body_mass_kg', 'body_fat_pct', 'lean_body_mass_kg', 'bmi', 'height_cm',
    'vo2max', 'walking_steadiness_pct',
}


def daily_column_order() -> List[str]:
    """Full ordered column list for daily_metrics.csv."""
    cols = ['date', 'wear_hours', 'wear_class']
    seen = set(cols)
    groups = ['sleep', 'cardio', 'vitals', 'activity', 'effort', 'body', 'mobility',
              'nutrition', 'environment', 'mind', 'other', 'quality']
    ordered: Dict[str, List[str]] = {g: [] for g in groups}

    for spec in DAILY_SPECS:
        ordered.setdefault(spec.group, []).append(spec.column)
    for col, group, _label in DERIVED_DAILY_COLUMNS:
        ordered.setdefault(group, []).append(col)

    for group in groups:
        for col in ordered.get(group, []):
            if col not in seen:
                cols.append(col)
                seen.add(col)
    return cols
