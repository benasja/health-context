#!/usr/bin/env python3
"""Generate a synthetic multi-year Apple Health export for demos and tests.

Why this exists
---------------
`sample_export.xml` is two days long. Two days is enough to prove the parser
runs and not enough to compute anything: every percentile, every 60-day
baseline and every 28-day best window in the output reads `n/a`. Someone
evaluating this project follows the quickstart, sees a report full of blanks,
and reasonably concludes the tool does not do much.

The parts of this pipeline worth looking at are exactly the parts that need
history. So this writes history: about two and a half years of daily data with
seasons, a training block, two illnesses, a holiday where the watch stayed at
home, and a stretch of bad sleep. Enough for baselines to settle, percentiles
to mean something, and the strain detector to have something real to find.

No health data is committed to this repository. The export is generated on
demand, and `.gitignore` already excludes `export.xml` at any depth. Output is
deterministic -- the same seed produces byte-identical XML -- so CI can assert
on specific numbers rather than merely checking the file is non-empty.

This is fabricated data. It is shaped to be plausible, not to be anyone's real
physiology, and nothing in it should be read as a reference range.

Usage
-----
    python3 fixtures/make_demo_export.py --out-dir demo
    python3 convert_health_data.py --data-dir demo --out-dir demo
"""

from __future__ import annotations

import argparse
import math
import os
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# Lithuania, where this was written. A non-UTC offset is deliberate: real
# exports carry local offsets, and a fixture pinned to +0000 would never catch
# a timezone bug in day-boundary handling.
TZ = '+0300'

WATCH = 'Apple Watch'
PHONE = 'iPhone'
SCALE = 'Withings'
# A third-party app installed once in 2023 and abandoned. It exists so the
# reliable-start detector has the scatter of early samples it was written for.
OLD_APP = 'Health Mate'

DEFAULT_START = date(2024, 1, 15)
DEFAULT_END = date(2026, 9, 1)
STRAY_START = date(2023, 10, 3)


def ts(d: date, hour: int, minute: int = 0, second: int = 0) -> str:
    """Apple's export timestamp format."""
    return f'{datetime(d.year, d.month, d.day, hour, minute, second):%Y-%m-%d %H:%M:%S} {TZ}'


def esc(value: str) -> str:
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


@dataclass
class Episode:
    """A named stretch of days that bends the numbers away from baseline."""

    name: str
    start: date
    end: date
    rhr: float = 0.0
    hrv_scale: float = 1.0
    resp: float = 0.0
    spo2: float = 0.0
    wrist_temp: float = 0.0
    sleep_scale: float = 1.0
    efficiency: float = 0.0
    step_scale: float = 1.0
    worn: bool = True

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


# The narrative. Each of these exists to exercise a specific branch of the
# analysis, not merely to add noise.
EPISODES: list[Episode] = [
    # Two or more of {temp up, RHR up, HRV down, resp up, SpO2 down} moving
    # together is what the strain detector looks for. This moves all five.
    Episode(
        'influenza', date(2024, 11, 18), date(2024, 11, 24),
        rhr=9.0, hrv_scale=0.62, resp=1.8, spo2=-1.1, wrist_temp=0.9,
        sleep_scale=1.12, efficiency=-7.0, step_scale=0.28,
    ),
    # Milder, and two years later -- so the same detector has to fire against a
    # baseline that has since drifted.
    Episode(
        'head cold', date(2026, 2, 10), date(2026, 2, 15),
        rhr=5.0, hrv_scale=0.78, resp=0.9, spo2=-0.5, wrist_temp=0.45,
        sleep_scale=1.04, efficiency=-3.5, step_scale=0.55,
    ),
    # Watch left at home. Wear-hour classification should mark these `none`
    # and keep them out of the averages rather than reading them as rest days.
    Episode('holiday, watch at home', date(2025, 7, 19), date(2025, 7, 27), worn=False),
    # Late nights, short and broken sleep, no illness markers. Sleep regularity
    # should degrade while recovery components that do not depend on sleep hold.
    Episode(
        'deadline fortnight', date(2025, 11, 30), date(2025, 12, 18),
        rhr=2.5, hrv_scale=0.86, sleep_scale=0.78, efficiency=-4.0, step_scale=0.82,
    ),
]


@dataclass
class Day:
    """Everything emitted for one calendar day."""

    day: date
    worn: bool
    rhr: float
    hrv: float
    resp: float
    spo2: float
    wrist_temp: float
    steps: int
    active_kcal: int
    basal_kcal: int
    flights: int
    distance_km: float
    sleep_minutes: int
    efficiency: float
    bedtime_hour: float
    workout: str | None
    workout_minutes: int
    records: list[str] = field(default_factory=list)


def season(day: date) -> float:
    """-1 at midwinter, +1 at midsummer. Northern hemisphere."""
    return -math.cos(2 * math.pi * (day.timetuple().tm_yday - 1) / 365.25)


def training_load(day: date) -> float:
    """0 at rest, 1 at the peak of the spring 2025 block."""
    block_start, block_peak, block_end = date(2025, 3, 3), date(2025, 6, 8), date(2025, 7, 13)
    if not (block_start <= day <= block_end):
        return 0.0
    if day <= block_peak:
        return (day - block_start).days / max((block_peak - block_start).days, 1)
    return 1.0 - (day - block_peak).days / max((block_end - block_peak).days, 1)


def build_day(day: date, rng: random.Random, fitness: float) -> Day:
    active = [e for e in EPISODES if e.covers(day)]
    worn = all(e.worn for e in active)
    weekend = day.weekday() >= 5
    s = season(day)
    load = training_load(day)

    # Resting HR drifts down as fitness accumulates and up in winter.
    rhr = 56.0 - 4.5 * fitness - 0.8 * s + rng.gauss(0, 1.6)
    hrv = (44.0 + 9.0 * fitness + 1.5 * s + rng.gauss(0, 4.2)) * 1.0
    resp = 14.4 + rng.gauss(0, 0.45)
    spo2 = 97.3 + rng.gauss(0, 0.5)
    wrist_temp = 35.15 + 0.25 * s + rng.gauss(0, 0.12)

    for e in active:
        rhr += e.rhr
        hrv *= e.hrv_scale
        resp += e.resp
        spo2 += e.spo2
        wrist_temp += e.wrist_temp

    # Steps: summer walking, weekend lulls, training-block commuting.
    steps = 8600 + 2300 * s + (900 if not weekend else -1400) + 1500 * load + rng.gauss(0, 1500)
    for e in active:
        steps *= e.step_scale
    steps = max(int(steps), 350)

    sleep = 432 + (46 if weekend else 0) + rng.gauss(0, 27)
    efficiency = 90.5 + rng.gauss(0, 2.4)
    bedtime = 23.05 + (0.85 if weekend else 0.0) + rng.gauss(0, 0.42)
    for e in active:
        sleep *= e.sleep_scale
        efficiency += e.efficiency
        if e.name == 'deadline fortnight':
            bedtime += 1.6
    sleep_minutes = int(max(min(sleep, 620), 205))
    efficiency = max(min(efficiency, 98.5), 68.0)

    workout, workout_minutes = None, 0
    # More sessions during the block; cycling is the primary sport.
    chance = 0.34 + 0.34 * load
    if worn and rng.random() < chance:
        workout = 'Cycling' if rng.random() < 0.68 else 'Running'
        base = 62 if workout == 'Cycling' else 38
        workout_minutes = int(base + 44 * load + rng.gauss(0, 12))
        workout_minutes = max(workout_minutes, 18)

    active_kcal = int(steps * 0.042 + workout_minutes * 8.6 + rng.gauss(0, 55))
    basal_kcal = int(1655 + rng.gauss(0, 28))

    return Day(
        day=day, worn=worn, rhr=rhr, hrv=max(hrv, 8.0), resp=resp, spo2=min(spo2, 100.0),
        wrist_temp=wrist_temp, steps=steps, active_kcal=max(active_kcal, 60), basal_kcal=basal_kcal,
        flights=max(int(6 + 3 * s + rng.gauss(0, 3)), 0),
        distance_km=round(steps * 0.00072, 2),
        sleep_minutes=sleep_minutes, efficiency=efficiency, bedtime_hour=bedtime,
        workout=workout, workout_minutes=workout_minutes,
    )


def quantity(kind: str, unit: str, value: str, source: str, start: str, end: str | None = None) -> str:
    return (
        f'  <Record type="HKQuantityTypeIdentifier{kind}" unit="{unit}" value="{value}" '
        f'sourceName="{esc(source)}" startDate="{start}" endDate="{end or start}"/>'
    )


def emit_sleep(out: list[str], d: Day, rng: random.Random) -> None:
    """One night, as the watch records it: an InBed span plus staged segments."""
    if not d.worn:
        return
    night = d.day - timedelta(days=1)
    bed_h = int(d.bedtime_hour)
    bed_m = int((d.bedtime_hour - bed_h) * 60)
    in_bed_minutes = int(d.sleep_minutes / (d.efficiency / 100.0))

    start_dt = datetime(night.year, night.month, night.day, min(bed_h, 23), bed_m)
    end_dt = start_dt + timedelta(minutes=in_bed_minutes)
    fmt = f'%Y-%m-%d %H:%M:%S {TZ}'
    out.append(
        f'  <Record type="HKCategoryTypeIdentifierSleepAnalysis" '
        f'value="HKCategoryValueSleepAnalysisInBed" sourceName="{WATCH}" '
        f'startDate="{start_dt:{fmt}}" endDate="{end_dt:{fmt}}"/>'
    )

    # Stage proportions: deep front-loaded, REM towards morning. Roughly the
    # shape Apple reports, which is what the sleep scoring expects to see.
    #
    # The shares vary night to night. Holding them constant would make deep and
    # REM share identical in every 28-day window, and their percentiles would
    # then rank pure floating-point noise -- which looks like signal in a table.
    deep = max(min(0.17 + rng.gauss(0, 0.028), 0.27), 0.08)
    rem = max(min(0.23 + rng.gauss(0, 0.034), 0.32), 0.12)
    awake = max(min(0.08 + rng.gauss(0, 0.02), 0.16), 0.02)
    stages = [
        ('AsleepCore', max(1.0 - deep - rem, 0.3)),
        ('AsleepDeep', deep),
        ('AsleepREM', rem),
        ('Awake', awake),
    ]
    cursor = start_dt + timedelta(minutes=9)
    for name, share in stages:
        minutes = int(d.sleep_minutes * share)
        if minutes <= 0:
            continue
        seg_end = cursor + timedelta(minutes=minutes)
        out.append(
            f'  <Record type="HKCategoryTypeIdentifierSleepAnalysis" '
            f'value="HKCategoryValueSleepAnalysis{name}" sourceName="{WATCH}" '
            f'startDate="{cursor:{fmt}}" endDate="{seg_end:{fmt}}"/>'
        )
        cursor = seg_end


def emit_steps(out: list[str], d: Day, rng: random.Random) -> None:
    """Steps from watch and phone, overlapping on purpose.

    Both devices see most of the same walking. Summing them inflates the total
    by roughly a third, which is the deduplication this project exists to do
    correctly -- so the fixture has to contain the problem.
    """
    buckets = [(8, 0.18), (12, 0.22), (15, 0.16), (18, 0.26), (20, 0.18)]
    for hour, share in buckets:
        watch_steps = int(d.steps * share)
        if watch_steps <= 0:
            continue
        if d.worn:
            out.append(quantity('StepCount', 'count', str(watch_steps), WATCH,
                                ts(d.day, hour, 0), ts(d.day, hour, 59)))
        # The phone sees a noisy subset, and is the only source on non-wear days.
        phone_steps = int(watch_steps * rng.uniform(0.72, 0.97))
        if phone_steps > 0:
            out.append(quantity('StepCount', 'count', str(phone_steps), PHONE,
                                ts(d.day, hour, 0), ts(d.day, hour, 59)))


def emit_workout(out: list[str], d: Day, rng: random.Random, max_hr: int) -> None:
    if not d.workout:
        return
    start_h = 17 if d.day.weekday() < 5 else 10
    fmt = f'%Y-%m-%d %H:%M:%S {TZ}'
    start_dt = datetime(d.day.year, d.day.month, d.day.day, start_h, 15)
    end_dt = start_dt + timedelta(minutes=d.workout_minutes)

    avg_hr = int(max_hr * (0.70 + 0.06 * training_load(d.day)) + rng.gauss(0, 4))
    peak_hr = min(int(avg_hr + rng.uniform(14, 26)), max_hr)
    kcal = int(d.workout_minutes * (9.1 if d.workout == 'Cycling' else 11.4))
    km = round(d.workout_minutes * (0.48 if d.workout == 'Cycling' else 0.19), 2)

    # Heart rate through the session, so the zone maths has a stream to bin.
    steps_n = max(d.workout_minutes // 6, 3)
    for i in range(steps_n):
        moment = start_dt + timedelta(minutes=i * 6)
        hr = int(avg_hr + rng.gauss(0, 9))
        out.append(quantity('HeartRate', 'count/min', str(max(min(hr, max_hr), 70)), WATCH,
                            f'{moment:{fmt}}'))
        if d.workout == 'Cycling':
            out.append(quantity('CyclingPower', 'W', str(int(196 + 38 * training_load(d.day) + rng.gauss(0, 22))),
                                'Wahoo KICKR', f'{moment:{fmt}}'))

    out.append(
        f'  <Workout workoutActivityType="HKWorkoutActivityType{d.workout}" '
        f'duration="{d.workout_minutes}" durationUnit="min" '
        f'totalEnergyBurned="{kcal}" totalEnergyBurnedUnit="Cal" '
        f'totalDistance="{km}" totalDistanceUnit="km" sourceName="{WATCH}" '
        f'startDate="{start_dt:{fmt}}" endDate="{end_dt:{fmt}}">'
    )
    out.append(f'    <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" '
               f'average="{avg_hr}" maximum="{peak_hr}" unit="count/min"/>')
    out.append(f'    <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" '
               f'sum="{kcal}" unit="kcal"/>')
    out.append('  </Workout>')


def emit_day(out: list[str], d: Day, rng: random.Random, max_hr: int) -> None:
    emit_sleep(out, d, rng)
    emit_steps(out, d, rng)

    out.append(quantity('BasalEnergyBurned', 'kcal', str(d.basal_kcal), WATCH,
                        ts(d.day, 0, 0), ts(d.day, 23, 59)))
    out.append(quantity('ActiveEnergyBurned', 'kcal', str(d.active_kcal), WATCH,
                        ts(d.day, 12, 0), ts(d.day, 12, 30)))
    out.append(quantity('DistanceWalkingRunning', 'km', f'{d.distance_km:.2f}', WATCH, ts(d.day, 12, 0)))
    out.append(quantity('FlightsClimbed', 'count', str(d.flights), PHONE, ts(d.day, 13, 0)))

    if not d.worn:
        # No wrist, no wrist-derived signal. The gap is the point.
        return

    out.append(quantity('RestingHeartRate', 'count/min', f'{d.rhr:.0f}', WATCH, ts(d.day, 7, 30)))
    out.append(quantity('RespiratoryRate', 'count/min', f'{d.resp:.1f}', WATCH, ts(d.day, 4, 10)))
    out.append(quantity('OxygenSaturation', '%', f'{d.spo2 / 100:.3f}', WATCH, ts(d.day, 4, 25)))
    out.append(quantity('AppleSleepingWristTemperature', 'degC', f'{d.wrist_temp:.2f}', WATCH, ts(d.day, 4, 40)))

    # HRV is sampled a few times a night, not once.
    for i in range(3):
        value = max(d.hrv + rng.gauss(0, 3.4), 6.0)
        out.append(quantity('HeartRateVariabilitySDNN', 'ms', f'{value:.1f}', WATCH,
                            ts(d.day, 3 + i, 12)))

    # Wear detection keys off types that need skin contact -- heart rate,
    # physical effort, stand *time*. A watch worn overnight reports heart rate
    # around the clock, sparsely while asleep and often while awake. Without
    # this the fixture looks like a watch that was never on a wrist.
    for hour in (0, 2, 4, 6):
        sleeping_hr = int(d.rhr + rng.gauss(0, 2.5))
        out.append(quantity('HeartRate', 'count/min', str(max(sleeping_hr, 38)), WATCH, ts(d.day, hour, 21)))
    for hour in range(7, 24):
        waking_hr = int(d.rhr + 14 + rng.gauss(0, 7))
        out.append(quantity('HeartRate', 'count/min', str(max(waking_hr, 45)), WATCH, ts(d.day, hour, 33)))

    # AppleStandTime is a quantity in minutes, distinct from the AppleStandHour
    # category; the wear signal reads the former.
    for hour in range(8, 22):
        stand_minutes = rng.choice([1, 2, 3, 5, 8])
        out.append(quantity('AppleStandTime', 'min', str(stand_minutes), WATCH,
                            ts(d.day, hour, 0), ts(d.day, hour, 59)))
        out.append(
            f'  <Record type="HKCategoryTypeIdentifierAppleStandHour" '
            f'value="HKCategoryValueAppleStandHour" sourceName="{WATCH}" '
            f'startDate="{ts(d.day, hour, 0)}" endDate="{ts(d.day, hour, 59)}"/>'
        )

    emit_workout(out, d, rng, max_hr)


def generate(start: date, end: date, seed: int) -> str:
    rng = random.Random(seed)
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE HealthData [',
        '<!ELEMENT HealthData (ExportDate,Me,(Record|Workout)*)>',
        ']>',
        '<HealthData locale="lt_LT">',
        f'  <ExportDate value="{ts(end + timedelta(days=1), 9, 30)}"/>',
        '  <Me HKCharacteristicTypeIdentifierDateOfBirth="1999-04-12" '
        'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>',
    ]

    # Stray samples from an app installed once in 2023 and forgotten. The
    # reliable-start detector should decline to treat these as the beginning of
    # the record: they are too sparse to support a baseline.
    stray = STRAY_START
    while stray < start - timedelta(days=20):
        out.append(quantity('StepCount', 'count', str(rng.randint(3200, 9100)), OLD_APP, ts(stray, 21, 0)))
        if rng.random() < 0.4:
            out.append(quantity('BodyMass', 'kg', f'{rng.uniform(81.5, 84.0):.1f}', OLD_APP, ts(stray, 7, 20)))
        stray += timedelta(days=rng.randint(9, 27))

    max_hr = 191
    total_days = (end - start).days + 1
    weight = 83.4
    vo2 = 41.2
    day = start

    for index in range(total_days):
        # Fitness accumulates through the training block and decays slowly after.
        fitness = min(index / 420.0, 1.0) * 0.55 + training_load(day) * 0.45
        d = build_day(day, rng, fitness)
        emit_day(out, d, rng, max_hr)

        # Weight: measured a few times a week, never daily. Slow loss through
        # the first year, a plateau, a small winter rise.
        if rng.random() < 0.42:
            drift = -6.4 * min(index / 500.0, 1.0) + 0.9 * max(-season(day), 0)
            weight = 83.4 + drift + rng.gauss(0, 0.32)
            out.append(quantity('BodyMass', 'kg', f'{weight:.1f}', SCALE, ts(day, 7, 15)))
            out.append(quantity('BodyFatPercentage', '%', f'{(0.208 - 0.024 * fitness + rng.gauss(0, 0.004)):.3f}',
                                SCALE, ts(day, 7, 16)))

        # VO2 max: the watch estimates it every week or so, not every day --
        # often enough that a 28-day window carries a few measurements.
        if rng.random() < 0.16:
            vo2 = 41.2 + 6.8 * fitness + rng.gauss(0, 0.5)
            out.append(quantity('VO2Max', 'ml/kg*min', f'{vo2:.1f}', WATCH, ts(day, 18, 40)))

        day += timedelta(days=1)

    out.append('</HealthData>')
    out.append('')
    return '\n'.join(out)


def parse_day(value: str) -> date:
    return datetime.strptime(value, '%Y-%m-%d').date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Write a deterministic synthetic Apple Health export for demos and tests.',
    )
    parser.add_argument('--out-dir', default='demo',
                        help='Directory to write export.xml into (created if missing). Default: demo')
    parser.add_argument('--start', type=parse_day, default=DEFAULT_START,
                        help=f'First day of continuous data. Default: {DEFAULT_START}')
    parser.add_argument('--end', type=parse_day, default=DEFAULT_END,
                        help=f'Last day of data. Default: {DEFAULT_END}')
    parser.add_argument('--seed', type=int, default=20260917,
                        help='Seed. The same seed always produces identical output.')
    args = parser.parse_args()

    if args.end <= args.start:
        parser.error('--end must be after --start')

    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, 'export.xml')
    xml = generate(args.start, args.end, args.seed)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(xml)

    days = (args.end - args.start).days + 1
    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f'Wrote {path}')
    print(f'  {days} days ({args.start} to {args.end}), {xml.count(chr(10)):,} lines, {size_mb:.1f} MB')
    print(f'  Episodes: {", ".join(e.name for e in EPISODES)}')
    print()
    print('Next:')
    print(f'  python3 convert_health_data.py --data-dir {args.out_dir} --out-dir {args.out_dir}')


if __name__ == '__main__':
    main()
