#!/usr/bin/env python3

"""Regression tests for the Apple Health converter.

Every test here corresponds to a bug that actually shipped and produced
plausible-looking wrong numbers rather than an error. Stdlib only:

    python3 test_health.py
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from health_coaching import (
    DEDUP_INTERVAL_TYPES,
    DEDUP_PRIMARY_TYPES,
    HealthRecord,
    Workout,
    aggregate_sleep_by_day,
    dedup_interval_sum,
    dedup_primary_sum,
    detect_coverage,
    energy_to_kcal,
    is_stand_hour_met,
    is_watch_source,
    local_calendar_date,
    normalize_source,
    observed_max_hr,
    resolve_max_hr,
    source_priority,
)
from health_insights import (
    build_series,
    carry_forward,
    circular_stats,
    compute_trends,
    illness_signals,
    pearson,
    rolling_baseline,
)
from health_metrics import (
    DAILY_SPECS,
    SPECS_BY_TYPE,
    WEAR_SIGNAL_TYPES,
    daily_column_order,
)

TZ = timezone(timedelta(hours=3))  # matches the sample export's +0300


def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def rec(rtype, value, source, start, end, unit='count', category=''):
    return HealthRecord(
        type=rtype, value=value, category_value=category, unit=unit,
        source_name=source, start=start, end=end, creation='',
        start_raw=start.isoformat(), end_raw=end.isoformat(),
    )


def sleep_rec(source, start, end, stage):
    return rec('HKCategoryTypeIdentifierSleepAnalysis', None, source, start, end,
               unit='', category='HKCategoryValueSleepAnalysis' + stage)


def spec_for(column):
    return next(s for s in DAILY_SPECS if s.column == column)


class TestPercentUnits(unittest.TestCase):
    """HealthKit's `%` unit is a fraction. Emitting it raw reported 13.6% body
    fat as 0.136, which reads as physiologically impossible."""

    def test_body_fat_scaled_to_percent(self):
        self.assertAlmostEqual(spec_for('body_fat_pct').convert(0.136, '%'), 13.6, places=6)

    def test_spo2_scaled_to_percent(self):
        self.assertAlmostEqual(spec_for('spo2_avg').convert(0.966, '%'), 96.6, places=6)

    def test_walking_steadiness_scaled(self):
        self.assertAlmostEqual(spec_for('walking_steadiness_pct').convert(0.954, '%'), 95.4, places=6)

    def test_every_percent_metric_is_scaled(self):
        # Guards against a new percent metric being added without scale=100.
        for spec in DAILY_SPECS:
            if spec.column.endswith('_pct') and spec.column != 'sleep_efficiency_pct':
                self.assertEqual(spec.scale, 100.0, f'{spec.column} missing scale=100')

    def test_non_percent_metric_untouched(self):
        self.assertAlmostEqual(spec_for('resting_hr').convert(61.0, 'count/min'), 61.0)


class TestUnitConversion(unittest.TestCase):
    def test_kilojoules_to_kcal(self):
        self.assertAlmostEqual(energy_to_kcal(418.4, 'kJ'), 100.0, places=4)

    def test_kcal_passthrough(self):
        self.assertAlmostEqual(energy_to_kcal(500.0, 'Cal'), 500.0)

    def test_metres_to_km(self):
        self.assertAlmostEqual(spec_for('distance_km').convert(1500.0, 'm'), 1.5)

    def test_pounds_to_kg(self):
        self.assertAlmostEqual(spec_for('body_mass_kg').convert(220.462, 'lb'), 100.0, places=2)


class TestSourceNormalization(unittest.TestCase):
    """The same watch appeared as three sources because of a non-breaking
    space in "Apple Watch"."""

    def test_non_breaking_space_folded(self):
        self.assertEqual(normalize_source('Benas’s Apple\xa0Watch'),
                         'Benas’s Apple Watch')

    def test_watch_detected_despite_nbsp(self):
        self.assertTrue(is_watch_source('Benas’s Apple\xa0Watch'))

    def test_iphone_is_not_watch(self):
        self.assertFalse(is_watch_source('Benas’s iPhone'))

    def test_priority_watch_beats_phone_beats_other(self):
        self.assertLess(source_priority('Apple Watch'), source_priority('Benas iPhone'))
        self.assertLess(source_priority('Benas iPhone'), source_priority('Power Sync'))


class TestMultiDeviceDedup(unittest.TestCase):
    """An iPhone in a pocket and a watch on a wrist count the same steps.
    Summing both inflated step totals by ~35%."""

    STEPS = 'HKQuantityTypeIdentifierStepCount'

    def test_overlapping_phone_records_dropped(self):
        records = [
            rec(self.STEPS, 1000.0, 'Apple Watch', dt(2026, 7, 1, 10), dt(2026, 7, 1, 11)),
            rec(self.STEPS, 600.0, 'Benas iPhone', dt(2026, 7, 1, 10), dt(2026, 7, 1, 11)),
        ]
        totals = dedup_interval_sum(records, self.STEPS, spec_for('steps').convert)
        self.assertAlmostEqual(totals[date(2026, 7, 1)], 1000.0)

    def test_phone_still_fills_uncovered_hours(self):
        records = [
            rec(self.STEPS, 1000.0, 'Apple Watch', dt(2026, 7, 1, 10), dt(2026, 7, 1, 11)),
            rec(self.STEPS, 600.0, 'Benas iPhone', dt(2026, 7, 1, 10), dt(2026, 7, 1, 11)),
            rec(self.STEPS, 500.0, 'Benas iPhone', dt(2026, 7, 1, 14), dt(2026, 7, 1, 15)),
        ]
        totals = dedup_interval_sum(records, self.STEPS, spec_for('steps').convert)
        self.assertAlmostEqual(totals[date(2026, 7, 1)], 1500.0)

    def test_partial_overlap_is_rejected_not_prorated(self):
        # A phone record straddling the watch window is dropped whole; the tool
        # never invents a fractional value it cannot justify.
        records = [
            rec(self.STEPS, 1000.0, 'Apple Watch', dt(2026, 7, 1, 10), dt(2026, 7, 1, 11)),
            rec(self.STEPS, 400.0, 'Benas iPhone', dt(2026, 7, 1, 10, 30), dt(2026, 7, 1, 11, 30)),
        ]
        totals = dedup_interval_sum(records, self.STEPS, spec_for('steps').convert)
        self.assertAlmostEqual(totals[date(2026, 7, 1)], 1000.0)

    def test_same_device_renamed_does_not_self_cancel(self):
        # "Benas iPhone" -> "Benas's iPhone" is one phone across a rename; its
        # records never overlap, so both must count.
        records = [
            rec(self.STEPS, 300.0, 'Benas iPhone', dt(2026, 7, 1, 9), dt(2026, 7, 1, 10)),
            rec(self.STEPS, 400.0, 'Benas’s iPhone', dt(2026, 7, 1, 11), dt(2026, 7, 1, 12)),
        ]
        totals = dedup_interval_sum(records, self.STEPS, spec_for('steps').convert)
        self.assertAlmostEqual(totals[date(2026, 7, 1)], 700.0)

    def test_days_are_kept_separate(self):
        records = [
            rec(self.STEPS, 1000.0, 'Apple Watch', dt(2026, 7, 1, 10), dt(2026, 7, 1, 11)),
            rec(self.STEPS, 2000.0, 'Apple Watch', dt(2026, 7, 2, 10), dt(2026, 7, 2, 11)),
        ]
        totals = dedup_interval_sum(records, self.STEPS, spec_for('steps').convert)
        self.assertAlmostEqual(totals[date(2026, 7, 1)], 1000.0)
        self.assertAlmostEqual(totals[date(2026, 7, 2)], 2000.0)

    def test_dietary_uses_single_primary_source(self):
        # Two food apps mirroring the same meals would otherwise double the day.
        kcal = 'HKQuantityTypeIdentifierDietaryEnergyConsumed'
        records = [
            rec(kcal, 800.0, 'MyFitnessPal', dt(2026, 7, 1, 8), dt(2026, 7, 1, 8), unit='Cal'),
            rec(kcal, 700.0, 'MyFitnessPal', dt(2026, 7, 1, 13), dt(2026, 7, 1, 13), unit='Cal'),
            rec(kcal, 1500.0, 'Power Sync', dt(2026, 7, 1, 20), dt(2026, 7, 1, 20), unit='Cal'),
        ]
        totals = dedup_primary_sum(records, kcal, spec_for('diet_kcal').convert)
        self.assertAlmostEqual(totals[date(2026, 7, 1)], 1500.0)  # MyFitnessPal: 2 entries wins

    def test_dedup_targets_are_registered_metrics(self):
        columns = {s.column for s in DAILY_SPECS}
        for mapping in (DEDUP_INTERVAL_TYPES, DEDUP_PRIMARY_TYPES):
            for hk_type, column in mapping.items():
                self.assertIn(column, columns, f'{column} not in registry')
                self.assertIn(hk_type, SPECS_BY_TYPE, f'{hk_type} not parsed')


class TestSleepSessions(unittest.TestCase):
    """Bucketing segments by end date split one night across two days,
    producing a ~24h window and a meaningless midpoint."""

    WATCH = 'Benas’s Apple Watch'

    def test_night_crossing_midnight_stays_one_session(self):
        records = [
            # night 20->21, ends on the 21st
            sleep_rec(self.WATCH, dt(2026, 7, 21, 0, 39), dt(2026, 7, 21, 7, 54), 'AsleepCore'),
            # night 21->22 begins before midnight and ends on the 22nd
            sleep_rec(self.WATCH, dt(2026, 7, 21, 23, 52), dt(2026, 7, 21, 23, 58), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 22, 0, 5), dt(2026, 7, 22, 7, 35), 'AsleepCore'),
        ]
        nights = aggregate_sleep_by_day(records)

        self.assertEqual(nights[date(2026, 7, 21)]['wake'].strftime('%H:%M'), '07:54')
        self.assertEqual(nights[date(2026, 7, 22)]['onset'].strftime('%H:%M'), '23:52')
        self.assertEqual(nights[date(2026, 7, 22)]['wake'].strftime('%H:%M'), '07:35')

    def test_midpoint_is_nocturnal_not_midday(self):
        records = [
            sleep_rec(self.WATCH, dt(2026, 7, 21, 0, 39), dt(2026, 7, 21, 7, 54), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 21, 23, 52), dt(2026, 7, 21, 23, 58), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 22, 0, 5), dt(2026, 7, 22, 7, 35), 'AsleepCore'),
        ]
        nights = aggregate_sleep_by_day(records)
        for day in (date(2026, 7, 21), date(2026, 7, 22)):
            mid = nights[day]['midpoint_hour']
            self.assertTrue(0 <= mid <= 8, f'{day} midpoint {mid} is not overnight')

    def test_duration_not_doubled(self):
        records = [
            sleep_rec(self.WATCH, dt(2026, 7, 21, 0, 39), dt(2026, 7, 21, 7, 54), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 21, 23, 52), dt(2026, 7, 21, 23, 58), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 22, 0, 5), dt(2026, 7, 22, 7, 35), 'AsleepCore'),
        ]
        nights = aggregate_sleep_by_day(records)
        self.assertAlmostEqual(nights[date(2026, 7, 21)]['asleep'], 7.25, places=2)
        self.assertAlmostEqual(nights[date(2026, 7, 22)]['asleep'], 7.6, places=1)

    def test_brief_awakening_does_not_split_the_night(self):
        records = [
            sleep_rec(self.WATCH, dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 2, 0), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 2, 0), dt(2026, 7, 23, 2, 20), 'Awake'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 2, 20), dt(2026, 7, 23, 7, 0), 'AsleepCore'),
        ]
        nights = aggregate_sleep_by_day(records)
        self.assertEqual(len(nights), 1)
        night = nights[date(2026, 7, 23)]
        self.assertEqual(night['onset'].strftime('%H:%M'), '23:00')
        self.assertEqual(night['wake'].strftime('%H:%M'), '07:00')
        self.assertEqual(night['awakenings'], 1)

    def test_nap_does_not_override_the_night(self):
        records = [
            sleep_rec(self.WATCH, dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 7, 0), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 14, 0), dt(2026, 7, 23, 14, 30), 'AsleepCore'),
        ]
        nights = aggregate_sleep_by_day(records)
        night = nights[date(2026, 7, 23)]
        self.assertAlmostEqual(night['asleep'], 8.0, places=2)
        self.assertEqual(night['onset'].strftime('%H:%M'), '23:00')

    def test_staged_source_beats_longer_unstaged_source(self):
        records = [
            sleep_rec('Power Sync', dt(2026, 7, 22, 22, 0), dt(2026, 7, 23, 7, 0),
                      'AsleepUnspecified'),
            sleep_rec(self.WATCH, dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 4, 0), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 4, 0), dt(2026, 7, 23, 5, 0), 'AsleepDeep'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 5, 0), dt(2026, 7, 23, 6, 30), 'AsleepREM'),
        ]
        nights = aggregate_sleep_by_day(records)
        night = nights[date(2026, 7, 23)]
        self.assertIn('Watch', night['source'])
        self.assertAlmostEqual(night['deep'], 1.0, places=2)
        self.assertAlmostEqual(night['rem'], 1.5, places=2)

    def test_sources_are_not_summed(self):
        records = [
            sleep_rec('Power Sync', dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 7, 0),
                      'AsleepUnspecified'),
            sleep_rec(self.WATCH, dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 7, 0), 'AsleepCore'),
        ]
        nights = aggregate_sleep_by_day(records)
        self.assertAlmostEqual(nights[date(2026, 7, 23)]['asleep'], 8.0, places=2)

    def test_unspecified_sleep_is_surfaced(self):
        records = [
            sleep_rec(self.WATCH, dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 7, 0),
                      'AsleepUnspecified'),
        ]
        night = aggregate_sleep_by_day(records)[date(2026, 7, 23)]
        self.assertAlmostEqual(night['unspecified'], 8.0, places=2)
        self.assertAlmostEqual(night['asleep'], 8.0, places=2)

    def test_in_bed_only_night_is_ignored(self):
        records = [sleep_rec('Benas’s iPhone', dt(2026, 7, 22, 23, 0),
                             dt(2026, 7, 23, 7, 0), 'InBed')]
        self.assertEqual(aggregate_sleep_by_day(records), {})

    def test_efficiency_is_a_percentage(self):
        records = [
            sleep_rec(self.WATCH, dt(2026, 7, 22, 23, 0), dt(2026, 7, 23, 3, 0), 'AsleepCore'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 3, 0), dt(2026, 7, 23, 4, 0), 'Awake'),
            sleep_rec(self.WATCH, dt(2026, 7, 23, 4, 0), dt(2026, 7, 23, 7, 0), 'AsleepCore'),
        ]
        night = aggregate_sleep_by_day(records)[date(2026, 7, 23)]
        self.assertAlmostEqual(night['efficiency'], 87.5, places=1)  # 7h asleep / 8h window


class TestStandHours(unittest.TestCase):
    """"applestandhour" is a substring of both Stood and Idle, so only the
    suffix distinguishes them."""

    def test_stood_counts(self):
        self.assertTrue(is_stand_hour_met('HKCategoryValueAppleStandHourStood'))

    def test_idle_does_not_count(self):
        self.assertFalse(is_stand_hour_met('HKCategoryValueAppleStandHourIdle'))

    def test_legacy_bare_value_counts(self):
        # Pre-iOS-13 exports predate the Idle case; a bare record is a met hour.
        self.assertTrue(is_stand_hour_met('HKCategoryValueAppleStandHour'))

    def test_empty_does_not_count(self):
        self.assertFalse(is_stand_hour_met(''))


class TestMaxHeartRate(unittest.TestCase):
    """Zones are only as good as the ceiling. Workout peaks matter, but a lone
    optical artifact must not set the ceiling for the whole history."""

    def _workout(self, peak):
        return Workout(
            workout_activity_type='HKWorkoutActivityTypeTraditionalStrengthTraining',
            start=dt(2026, 5, 1, 10), end=dt(2026, 5, 1, 11),
            start_raw='', end_raw='', creation='', duration=60.0, duration_unit='min',
            total_energy=None, total_energy_unit='Cal', total_distance=None,
            total_distance_unit='', source_name='Apple Watch',
            statistics={'HKQuantityTypeIdentifierHeartRate': {
                'average': None, 'minimum': None, 'maximum': peak, 'sum': None,
                'unit': 'count/min'}},
        )

    def test_workout_peak_used_when_corroborated(self):
        samples = [rec('HKQuantityTypeIdentifierHeartRate', 140.0, 'Apple Watch',
                       dt(2026, 5, 1, 10, 5), dt(2026, 5, 1, 10, 6))]
        chosen, absolute, note = observed_max_hr(samples, [self._workout(145.0)])
        self.assertEqual(chosen, 145.0)
        self.assertEqual(absolute, 145.0)
        self.assertEqual(note, '')

    def test_isolated_outlier_peak_rejected(self):
        samples = [rec('HKQuantityTypeIdentifierHeartRate', 177.0, 'Apple Watch',
                       dt(2026, 5, 1, 10, 5), dt(2026, 5, 1, 10, 6))]
        workouts = [self._workout(198.0), self._workout(181.0)]
        chosen, absolute, note = observed_max_hr(samples, workouts)
        self.assertEqual(chosen, 181.0)
        self.assertEqual(absolute, 198.0)
        self.assertIn('198', note)

    def test_explicit_override_wins(self):
        samples = [rec('HKQuantityTypeIdentifierHeartRate', 177.0, 'Apple Watch',
                       dt(2026, 5, 1, 10), dt(2026, 5, 1, 10, 1))]
        max_hr, note = resolve_max_hr(samples, configured=190.0)
        self.assertEqual(max_hr, 190.0)
        self.assertIn('configured', note)

    def test_no_data_falls_back_to_default(self):
        max_hr, note = resolve_max_hr([], None, None)
        self.assertEqual(max_hr, 190.0)
        self.assertIn('default', note)


class TestWearSignals(unittest.TestCase):
    """Basal energy is modelled from BMR and emitted whether or not the watch
    is worn; counting it marked every day as fully worn."""

    def test_basal_energy_excluded(self):
        self.assertNotIn('HKQuantityTypeIdentifierBasalEnergyBurned', WEAR_SIGNAL_TYPES)

    def test_active_energy_excluded(self):
        self.assertNotIn('HKQuantityTypeIdentifierActiveEnergyBurned', WEAR_SIGNAL_TYPES)

    def test_skin_contact_signals_included(self):
        self.assertIn('HKQuantityTypeIdentifierHeartRate', WEAR_SIGNAL_TYPES)
        self.assertIn('HKQuantityTypeIdentifierPhysicalEffort', WEAR_SIGNAL_TYPES)


class TestLocalDate(unittest.TestCase):
    """Apple writes wall-clock time with an offset; converting to the machine's
    timezone shifted late-evening samples into the next day."""

    def test_late_evening_stays_on_its_own_day(self):
        self.assertEqual(local_calendar_date(dt(2026, 7, 21, 23, 30)), date(2026, 7, 21))

    def test_just_after_midnight_is_the_new_day(self):
        self.assertEqual(local_calendar_date(dt(2026, 7, 22, 0, 15)), date(2026, 7, 22))

    def test_far_flung_offset_uses_its_own_wall_clock(self):
        tokyo = datetime(2026, 7, 21, 23, 30, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(local_calendar_date(tokyo), date(2026, 7, 21))


class TestCircularStatistics(unittest.TestCase):
    """Sleep midpoints straddle midnight; 23.9 and 0.1 are 24 minutes apart,
    not 23.8 hours."""

    def test_times_either_side_of_midnight_are_close(self):
        mean_hour, sd_hours = circular_stats([23.9, 0.1, 23.8, 0.2])
        self.assertLess(sd_hours, 0.5)
        self.assertTrue(mean_hour > 23.5 or mean_hour < 0.5)

    def test_genuinely_scattered_times_report_high_spread(self):
        _mean, sd_hours = circular_stats([2.0, 8.0, 14.0, 20.0])
        self.assertGreater(sd_hours, 2.0)

    def test_identical_times_have_no_spread(self):
        _mean, sd_hours = circular_stats([4.0, 4.0, 4.0])
        self.assertAlmostEqual(sd_hours, 0.0, places=6)

    def test_too_few_points_returns_none(self):
        self.assertIsNone(circular_stats([4.0]))


class TestBaselines(unittest.TestCase):
    def test_baseline_excludes_the_day_itself(self):
        # A baseline containing today partly cancels the deviation it measures.
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
        series = dict.fromkeys(days[:-1], 50.0)
        series[days[-1]] = 200.0  # a spike on the final day
        baselines = rolling_baseline(series, days, window=30, min_n=5)
        mean, _sd = baselines[days[-1]]
        self.assertAlmostEqual(mean, 50.0, places=6)

    def test_baseline_withheld_until_enough_history(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        series = dict.fromkeys(days, 50.0)
        baselines = rolling_baseline(series, days, window=30, min_n=14)
        self.assertEqual(baselines, {})

    def test_carry_forward_stops_after_a_long_gap(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(90)]
        filled = carry_forward({days[0]: 76.0}, days, max_gap_days=45)
        self.assertAlmostEqual(filled[days[10]], 76.0)
        self.assertIn(days[45], filled)
        self.assertNotIn(days[60], filled)


class TestIllnessSignals(unittest.TestCase):
    """The wrist-temperature lookup used the wrong key, so the strongest
    illness signal silently never contributed."""

    def test_wrist_temperature_uses_the_emitted_column_name(self):
        signals = illness_signals({'wrist_temp_c_dev': 0.5})
        self.assertEqual(len(signals), 1)
        self.assertIn('wrist temp', signals[0])

    def test_resting_hr_needs_both_absolute_and_relative_rise(self):
        self.assertEqual(illness_signals({'resting_hr_dev': 5.0, 'resting_hr_z': 0.2}), [])
        self.assertEqual(len(illness_signals({'resting_hr_dev': 5.0, 'resting_hr_z': 1.5})), 1)

    def test_depressed_hrv_flags(self):
        self.assertEqual(len(illness_signals({'hrv_sdnn_z': -1.4})), 1)

    def test_healthy_day_produces_no_signals(self):
        self.assertEqual(illness_signals({
            'wrist_temp_c_dev': 0.05, 'resting_hr_dev': 0.5, 'resting_hr_z': 0.1,
            'hrv_sdnn_z': 0.3, 'respiratory_rate_dev': 0.1, 'respiratory_rate_z': 0.2,
            'spo2_avg_z': 0.4,
        }), [])

    def test_signals_combine(self):
        signals = illness_signals({
            'wrist_temp_c_dev': 0.6, 'hrv_sdnn_z': -2.0,
            'respiratory_rate_dev': 1.5, 'respiratory_rate_z': 1.5,
        })
        self.assertEqual(len(signals), 3)


class TestTrendGating(unittest.TestCase):
    """Carrying a weigh-in forward turned two measurements into a confident
    multi-week trend: a scale swap read as -31% body fat."""

    def _rows(self, body_fat_days):
        rows = []
        for i in range(150):
            d = date(2026, 3, 1) + timedelta(days=i)
            row = {'date': d.isoformat(), 'resting_hr': '60'}
            if d in body_fat_days:
                row['body_fat_pct'] = str(body_fat_days[d])
            rows.append(row)
        return rows

    def test_sparse_body_fat_trend_is_withheld(self):
        rows = self._rows({
            date(2026, 5, 14): 21.2, date(2026, 5, 15): 20.4, date(2026, 5, 18): 21.0,
            date(2026, 7, 13): 12.5, date(2026, 7, 14): 10.1,
        })
        trends, withheld = compute_trends(rows, None)
        reported = {t['metric'] for t in trends}
        self.assertNotIn('body_fat_pct', reported)
        self.assertIn('body_fat_pct', {w['metric'] for w in withheld})

    def test_well_measured_metric_is_reported(self):
        rows = self._rows({})
        trends, _withheld = compute_trends(rows, None)
        self.assertIn('resting_hr', {t['metric'] for t in trends})

    def test_reported_trend_counts_real_measurements(self):
        rows = self._rows({})
        trends, _ = compute_trends(rows, None)
        rhr = next(t for t in trends if t['metric'] == 'resting_hr')
        self.assertEqual(rhr['recent_measured'], rhr['recent_n'])


class TestCoverageDetection(unittest.TestCase):
    """A metric's usable window starts where it is sustainably tracked, not at
    a stray sample years earlier."""

    def test_reliable_start_skips_an_early_scatter(self):
        rows = []
        # three isolated samples in 2021, then daily tracking from 2024-10-16
        for stray in (date(2021, 5, 1), date(2021, 8, 3), date(2022, 1, 9)):
            rows.append({'date': stray.isoformat(), 'hrv_sdnn': '50'})
        for i in range(120):
            d = date(2024, 10, 16) + timedelta(days=i)
            rows.append({'date': d.isoformat(), 'hrv_sdnn': '55'})
        rows.sort(key=lambda r: r['date'])

        coverage = detect_coverage(rows, ['hrv_sdnn'])
        entry = coverage[0]
        self.assertEqual(entry['first_date'], '2021-05-01')
        self.assertEqual(entry['reliable_start'], '2024-10-16')

    def test_sparse_by_design_metric_keeps_its_first_date(self):
        rows = [{'date': (date(2024, 1, 1) + timedelta(days=i * 40)).isoformat(),
                 'body_fat_pct': '20'} for i in range(6)]
        coverage = detect_coverage(rows, ['body_fat_pct'])
        entry = coverage[0]
        self.assertEqual(entry['sparse_by_design'], 'yes')
        self.assertEqual(entry['reliable_start'], entry['first_date'])


class TestStatistics(unittest.TestCase):
    def test_perfect_positive_correlation(self):
        self.assertAlmostEqual(pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0, places=6)

    def test_perfect_negative_correlation(self):
        self.assertAlmostEqual(pearson([1, 2, 3, 4], [8, 6, 4, 2]), -1.0, places=6)

    def test_constant_series_has_no_correlation(self):
        self.assertIsNone(pearson([1, 1, 1, 1], [1, 2, 3, 4]))

    def test_build_series_treats_blank_as_missing(self):
        rows = [
            {'date': '2026-01-01', 'steps': '100'},
            {'date': '2026-01-02', 'steps': ''},
            {'date': '2026-01-03', 'steps': '300'},
        ]
        series = build_series(rows, 'steps')
        self.assertEqual(set(series), {date(2026, 1, 1), date(2026, 1, 3)})

    def test_blank_is_not_read_as_zero(self):
        rows = [{'date': '2026-01-01', 'steps': ''}]
        self.assertEqual(build_series(rows, 'steps'), {})


class TestRegistryIntegrity(unittest.TestCase):
    def test_no_duplicate_columns(self):
        columns = [s.column for s in DAILY_SPECS]
        self.assertEqual(len(columns), len(set(columns)))

    def test_every_column_appears_in_the_csv_header(self):
        header = set(daily_column_order())
        for spec in DAILY_SPECS:
            self.assertIn(spec.column, header, f'{spec.column} missing from CSV header')

    def test_header_has_no_duplicates(self):
        order = daily_column_order()
        self.assertEqual(len(order), len(set(order)))

    def test_aggregations_are_supported(self):
        for spec in DAILY_SPECS:
            self.assertIn(spec.agg, {'sum', 'mean', 'min', 'max', 'latest'})

    def test_date_is_the_first_column(self):
        self.assertEqual(daily_column_order()[0], 'date')


if __name__ == '__main__':
    unittest.main(verbosity=2)
