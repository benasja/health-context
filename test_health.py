#!/usr/bin/env python3

"""Regression tests for the Apple Health converter.

Every test here corresponds to a bug that actually shipped and produced
plausible-looking wrong numbers rather than an error. Stdlib only:

    python3 test_health.py
"""

import csv
import json
import os
import plistlib
import tempfile
import zipfile
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
from health_history import (
    METRICS_BY_COLUMN,
    StreakRule,
    WorkoutSession,
    band_edge_distance,
    build_history,
    build_metric_series,
    compute_capacity,
    compute_distribution,
    compute_records,
    compute_streak,
    detect_events,
    episodes_by_severity,
    group_strain_episodes,
    modality_breakdown,
    month_by_year_grid,
    percentile,
    power_summary,
    progression_target,
    segment_eras,
    signal_kind,
    workout_blackouts,
    zone_totals,
)
from health_insights import (
    ACWR_MIN_CHRONIC_LOAD,
    build_daily_insights,
    build_series,
    carry_forward,
    circular_stats,
    compute_trends,
    illness_signals,
    isolated_spikes,
    pearson,
    render_llm_context,
    rolling_baseline,
)
import health_ingest
import health_mcp
from health_mcp import (
    DEFAULT_PROTOCOL,
    HANDLERS,
    TOOLS,
    HealthData,
    _compare,
    _metric_stats,
    client_config,
    handle,
    ordinal,
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


HISTORY_START = date(2025, 1, 1)


def hist_rows(**columns):
    """Daily rows from parallel column lists. `None` writes a blank cell."""
    length = max(len(v) for v in columns.values())
    rows = []
    for i in range(length):
        row = {'date': (HISTORY_START + timedelta(days=i)).isoformat()}
        for col, values in columns.items():
            value = values[i] if i < len(values) else None
            row[col] = '' if value is None else value
        rows.append(row)
    return rows


def day_list(n, start=HISTORY_START):
    return [start + timedelta(days=k) for k in range(n)]


class TestHistorySeries(unittest.TestCase):
    def test_a_blank_cumulative_day_is_never_imputed_as_zero(self):
        # An earlier version zero-filled these, which put 150 fabricated winter
        # zeros into the daylight series and understated January by 3.4x.
        # HealthKit cannot distinguish "nothing happened" from "nothing was
        # recorded", so neither may this.
        rows = hist_rows(exercise_minutes=[30, None, 10])
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        series = build_metric_series(by_date, METRICS_BY_COLUMN['exercise_minutes'], day_list(3))
        self.assertNotIn(HISTORY_START + timedelta(days=1), series)
        self.assertEqual(len(series), 2)

    def test_a_measured_zero_survives(self):
        # The flip side: an explicit 0 is data and must not be dropped.
        rows = hist_rows(exercise_minutes=[30, 0, 10])
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        series = build_metric_series(by_date, METRICS_BY_COLUMN['exercise_minutes'], day_list(3))
        self.assertEqual(series[HISTORY_START + timedelta(days=1)], 0.0)

    def test_a_suspect_day_is_dropped_from_its_own_column_only(self):
        rows = hist_rows(resting_hr=[60, 92, 61], hrv_sdnn=[50, 51, 52])
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        spike = HISTORY_START + timedelta(days=1)
        rhr = build_metric_series(by_date, METRICS_BY_COLUMN['resting_hr'],
                                  day_list(3), [('resting_hr', spike)])
        hrv = build_metric_series(by_date, METRICS_BY_COLUMN['hrv_sdnn'],
                                  day_list(3), [('resting_hr', spike)])
        self.assertNotIn(spike, rhr)
        self.assertIn(spike, hrv)

    def test_distribution_counts_missing_days_and_floors_on_measured_values(self):
        rows = hist_rows(exercise_minutes=[30, None, 10])
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        metric = METRICS_BY_COLUMN['exercise_minutes']
        dist = compute_distribution(build_metric_series(by_date, metric, day_list(3)), metric, 3)
        self.assertEqual(dist.n_days, 2)
        self.assertEqual(dist.days_missing, 1)
        self.assertEqual(dist.lowest, 10.0)   # not 0.0

    def test_blank_day_stays_missing_for_a_reading(self):
        rows = hist_rows(hrv_sdnn=[50, None, 60])
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        series = build_metric_series(by_date, METRICS_BY_COLUMN['hrv_sdnn'], day_list(3))
        self.assertNotIn(HISTORY_START + timedelta(days=1), series)

    def test_a_day_with_no_row_at_all_is_never_zero_filled(self):
        rows = hist_rows(exercise_minutes=[30, 30, 30])
        del rows[1]
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        series = build_metric_series(by_date, METRICS_BY_COLUMN['exercise_minutes'], day_list(3))
        self.assertNotIn(HISTORY_START + timedelta(days=1), series)


class TestPersonalRecords(unittest.TestCase):
    def _records(self, column, values):
        rows = hist_rows(**{column: values})
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        metric = METRICS_BY_COLUMN[column]
        days = day_list(len(values))
        return {r.scope: r for r in compute_records(build_metric_series(by_date, metric, days),
                                                    days, metric)}

    def test_ties_report_the_earliest_day_and_count_the_rest(self):
        records = self._records('exercise_minutes', [90, 20, 90, 10])
        self.assertEqual(records['day'].period, '2025-01-01')
        self.assertEqual(records['day'].ties, 1)

    def test_lower_is_better_metric_records_its_minimum(self):
        records = self._records('resting_hr', [60, 52, 58, 61])
        self.assertEqual(records['day'].per_day, 52)
        self.assertEqual(records['day'].period, '2025-01-02')

    def test_rolling_window_record_reports_mean_and_total(self):
        # 10 quiet days, then a hard week.
        records = self._records('exercise_minutes', [0] * 10 + [60] * 7)
        self.assertAlmostEqual(records['7d'].per_day, 60.0)
        self.assertAlmostEqual(records['7d'].total, 420.0)
        self.assertEqual(records['7d'].period, '2025-01-11..2025-01-17')

    def test_calendar_scopes_are_gone(self):
        # ISO week and calendar month were dropped: a best ISO week is a best
        # rolling 7 days snapped to an arbitrary grid, and it reported the same
        # week one day offset for a fifth of the pack's tokens.
        records = self._records('exercise_minutes', [40] * 60)
        self.assertEqual(set(records), {'day', '7d', '28d'})

    def test_a_record_window_reports_how_many_of_its_days_were_measured(self):
        # The first window holds only five measured days but the same mean, and
        # ties go to the earliest — so the record must disclose 5/7 rather than
        # let a partly-measured window pass as a full one.
        records = self._records('exercise_minutes', [None, None] + [60] * 30)
        self.assertEqual(records['7d'].observations, 5)
        self.assertEqual(records['7d'].span, 7)
        self.assertAlmostEqual(records['7d'].per_day, 60.0)

    def test_thin_coverage_blocks_a_window_record(self):
        # Two measured nights inside a week is not a week of sleep.
        values = [None] * 5 + [9.0, 9.0] + [7.0] * 21
        records = self._records('sleep_asleep_hours', values)
        self.assertNotEqual(records['7d'].period, '2025-01-01..2025-01-07')

    def test_empty_metric_produces_no_records(self):
        rows = hist_rows(exercise_minutes=[1, 2])
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        metric = METRICS_BY_COLUMN['vo2max']
        days = day_list(2)
        series = build_metric_series(by_date, metric, days)
        self.assertEqual(compute_records(series, days, metric), [])


class TestCapacityGap(unittest.TestCase):
    def _capacity(self, column, values):
        rows = hist_rows(**{column: values})
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        metric = METRICS_BY_COLUMN[column]
        days = day_list(len(values))
        return compute_capacity(build_metric_series(by_date, metric, days), days, metric)

    def test_current_is_measured_against_the_best_sustained_block(self):
        cap = self._capacity('exercise_minutes', [100] * 28 + [10] * 28)
        self.assertAlmostEqual(cap.current, 10.0)
        self.assertAlmostEqual(cap.best, 100.0)
        self.assertEqual(cap.best_period, '2025-01-01..2025-01-28')
        self.assertAlmostEqual(cap.pct_of_peak, 10.0)

    def test_percent_of_peak_is_withheld_where_zero_is_unreachable(self):
        # A resting-HR ratio is trapped near 100% by construction: at the worst
        # 28 days of the record it would still read "93% of peak" and be taken
        # as fine. The percentile carries that information honestly instead.
        cap = self._capacity('resting_hr', [50] * 28 + [60] * 28)
        self.assertAlmostEqual(cap.best, 50.0)
        self.assertFalse(cap.zero_floored)
        self.assertIsNone(cap.pct_of_peak)
        self.assertIsNotNone(cap.percentile)

    def test_percent_of_peak_is_kept_for_zero_floored_metrics(self):
        cap = self._capacity('exercise_minutes', [100] * 28 + [10] * 28)
        self.assertTrue(cap.zero_floored)
        self.assertAlmostEqual(cap.pct_of_peak, 10.0)

    def test_a_record_shorter_than_the_window_still_reports_current(self):
        cap = self._capacity('exercise_minutes', [20, 40])
        self.assertAlmostEqual(cap.current, 30.0)
        self.assertIsNone(cap.best)          # no full 28-day window exists yet
        self.assertIsNone(cap.pct_of_peak)
        self.assertEqual(cap.windows, 0)

    def test_percentile_places_the_current_window_in_its_own_history(self):
        cap = self._capacity('exercise_minutes', [100] * 28 + [10] * 28)
        self.assertLess(cap.percentile, 20.0)


class TestEraSegmentation(unittest.TestCase):
    def _eras(self, values, **kwargs):
        rows = hist_rows(exercise_minutes=values)
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        days = day_list(len(values))
        series = build_metric_series(by_date, METRICS_BY_COLUMN['exercise_minutes'], days)
        return segment_eras(series, days, {}, **kwargs)

    def test_a_regime_change_becomes_two_eras(self):
        eras = self._eras([80] * 60 + [0] * 60)
        self.assertEqual(len(eras), 2)
        self.assertEqual([e.band for e in eras], ['peak', 'dormant'])
        self.assertEqual(eras[0].start, HISTORY_START)
        self.assertEqual(eras[1].end, HISTORY_START + timedelta(days=119))
        self.assertEqual(eras[0].days + eras[1].days, 120)

    def test_a_steady_record_is_one_era(self):
        eras = self._eras([40] * 90)
        self.assertEqual(len(eras), 1)
        self.assertEqual(eras[0].band, 'active')

    def test_a_stretch_below_the_minimum_is_absorbed_into_its_longer_neighbour(self):
        # A brief burst inside a long steady stretch is a wobble, not a regime.
        eras = self._eras([40] * 45 + [300] * 5 + [40] * 45, min_era_days=60)
        self.assertEqual(len(eras), 1)
        self.assertEqual(eras[0].days, 95)

    def test_adjacent_eras_never_share_a_band(self):
        eras = self._eras([80] * 50 + [0] * 50 + [80] * 50)
        bands = [e.band for e in eras]
        self.assertEqual(bands, ['peak', 'dormant', 'peak'])
        for earlier, later in zip(bands, bands[1:], strict=False):
            self.assertNotEqual(earlier, later)

    def test_eras_cover_every_day_without_overlap(self):
        eras = self._eras([80] * 40 + [5] * 40 + [45] * 40)
        self.assertEqual(eras[0].start, HISTORY_START)
        for earlier, later in zip(eras, eras[1:], strict=False):
            self.assertEqual(later.start - earlier.end, timedelta(days=1))
        self.assertEqual(sum(e.days for e in eras), 120)

    def test_no_load_data_produces_no_eras(self):
        self.assertEqual(segment_eras({}, day_list(30), {}), [])

    def test_era_context_averages_only_the_days_in_that_era(self):
        rows = hist_rows(exercise_minutes=[80] * 60 + [0] * 60,
                         hrv_sdnn=[70] * 60 + [40] * 60)
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        days = day_list(120)
        load = build_metric_series(by_date, METRICS_BY_COLUMN['exercise_minutes'], days)
        hrv = build_metric_series(by_date, METRICS_BY_COLUMN['hrv_sdnn'], days)
        eras = segment_eras(load, days, {'hrv_sdnn': hrv})
        self.assertGreater(eras[0].context['hrv_sdnn'], eras[-1].context['hrv_sdnn'])


class TestStreaks(unittest.TestCase):
    def _streak(self, column, values, threshold):
        rows = hist_rows(**{column: values})
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        metric = METRICS_BY_COLUMN[column]
        days = day_list(len(values))
        series = build_metric_series(by_date, metric, days)
        return compute_streak(series, days, StreakRule(column, threshold, 'test'))

    def test_longest_streak_reports_its_own_dates(self):
        streak = self._streak('exercise_minutes', [40, 40, 40, 0, 40, 40], 30)
        self.assertEqual(streak.longest, 3)
        self.assertEqual(streak.longest_start, HISTORY_START)
        self.assertEqual(streak.longest_end, HISTORY_START + timedelta(days=2))
        self.assertEqual(streak.days_met, 5)

    def test_current_streak_counts_only_the_tail(self):
        streak = self._streak('exercise_minutes', [40, 40, 40, 0, 40, 40], 30)
        self.assertEqual(streak.current, 2)

    def test_a_streak_broken_on_the_last_day_reports_zero(self):
        streak = self._streak('exercise_minutes', [40, 40, 0], 30)
        self.assertEqual(streak.current, 0)
        self.assertEqual(streak.longest, 2)

    def test_an_unmeasured_night_breaks_a_sleep_streak(self):
        # Levels are not zero-filled, so the blank is a gap, not a bad night —
        # but it is still not evidence of seven hours' sleep.
        streak = self._streak('sleep_asleep_hours', [8, 8, None, 8], 7)
        self.assertEqual(streak.longest, 2)
        self.assertEqual(streak.current, 1)

    def test_the_whole_record_can_be_one_streak(self):
        streak = self._streak('exercise_minutes', [40] * 10, 30)
        self.assertEqual(streak.longest, 10)
        self.assertEqual(streak.current, 10)

    def test_a_value_exactly_on_the_threshold_counts(self):
        streak = self._streak('exercise_minutes', [30, 30], 30)
        self.assertEqual(streak.longest, 2)


class TestStrainEpisodes(unittest.TestCase):
    @staticmethod
    def flagged(day, detail, count=2):
        return {'date': day, 'strain_flag': 'yes', 'strain_signal_count': count,
                'strain_detail': detail}

    def test_flagged_days_across_a_short_gap_are_one_episode(self):
        rows = [self.flagged('2025-03-01', 'resting HR +4.0 bpm; HRV -1.2 SD below baseline'),
                {'date': '2025-03-02', 'strain_flag': '', 'strain_detail': ''},
                {'date': '2025-03-03', 'strain_flag': '', 'strain_detail': ''},
                self.flagged('2025-03-04', 'wrist temp +0.50degC; HRV -1.5 SD below baseline')]
        episodes = group_strain_episodes(rows)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].span_days, 4)
        self.assertEqual(episodes[0].flagged_days, 2)

    def test_a_longer_gap_splits_the_episode(self):
        rows = [self.flagged('2025-03-01', 'resting HR +4.0 bpm; HRV -1.2 SD'),
                self.flagged('2025-03-06', 'resting HR +4.0 bpm; HRV -1.2 SD')]
        episodes = group_strain_episodes(rows)
        self.assertEqual(len(episodes), 2)

    def test_episode_keeps_the_peak_signal_count_and_the_union_of_signals(self):
        rows = [self.flagged('2025-03-01', 'resting HR +4.0 bpm; HRV -1.2 SD', count=2),
                self.flagged('2025-03-02', 'wrist temp +0.60degC; SpO2 -1.8 SD', count=4)]
        episode = group_strain_episodes(rows)[0]
        self.assertEqual(episode.peak_signals, 4)
        self.assertEqual(episode.signals, ['resting HR', 'HRV', 'wrist temp', 'SpO2'])

    def test_unflagged_rows_are_ignored(self):
        rows = [{'date': '2025-03-01', 'strain_flag': '', 'strain_detail': 'HRV -1.0 SD'}]
        self.assertEqual(group_strain_episodes(rows), [])

    def test_signal_kind_drops_the_measurement(self):
        self.assertEqual(signal_kind('wrist temp +0.45degC'), 'wrist temp')
        self.assertEqual(signal_kind('HRV -1.2 SD below baseline'), 'HRV')
        self.assertEqual(signal_kind('respiratory rate +1.1/min'), 'respiratory rate')
        self.assertEqual(signal_kind('resting HR +3.0 bpm'), 'resting HR')


def session(day_offset, activity, minutes=30.0, zones=None):
    return WorkoutSession(day=HISTORY_START + timedelta(days=day_offset),
                          activity=activity, minutes=minutes, zones=zones or {})


class TestWorkoutModality(unittest.TestCase):
    def test_share_is_by_minutes_not_by_session_count(self):
        # One long rehab walk and one short interval session are one session
        # each and are not the same training.
        sessions = [session(0, 'Cooldown', 90.0), session(1, 'HIIT', 10.0)]
        shares = modality_breakdown(sessions)
        self.assertEqual(shares[0].activity, 'Cooldown')
        self.assertAlmostEqual(shares[0].share_pct, 90.0)

    def test_breakdown_can_be_scoped_to_a_period(self):
        sessions = [session(0, 'Strength', 60.0), session(40, 'Cooldown', 60.0)]
        shares = modality_breakdown(sessions, HISTORY_START + timedelta(days=30),
                                    HISTORY_START + timedelta(days=50))
        self.assertEqual([s.activity for s in shares], ['Cooldown'])

    def test_zone_totals_sum_only_the_requested_window(self):
        sessions = [session(0, 'Ride', 60.0, {'z2': 40.0, 'z4': 20.0}),
                    session(40, 'Ride', 60.0, {'z2': 10.0})]
        self.assertAlmostEqual(zone_totals(sessions)['z2'], 50.0)
        self.assertAlmostEqual(
            zone_totals(sessions, HISTORY_START, HISTORY_START + timedelta(days=10))['z2'], 40.0)

    def test_empty_workout_list_is_not_a_division_by_zero(self):
        self.assertEqual(modality_breakdown([]), [])
        self.assertEqual(zone_totals([]), dict.fromkeys(('z1', 'z2', 'z3', 'z4', 'z5'), 0.0))


class TestWorkoutBlackouts(unittest.TestCase):
    def test_a_long_gap_between_workouts_is_reported(self):
        sessions = [session(0, 'Strength'), session(40, 'Strength')]
        gaps = workout_blackouts(sessions, day_list(41))
        self.assertEqual(gaps[0][2], 39)
        self.assertEqual(gaps[0][0], HISTORY_START + timedelta(days=1))

    def test_short_gaps_are_ignored(self):
        sessions = [session(i, 'Strength') for i in range(0, 20, 3)]
        self.assertEqual(workout_blackouts(sessions, day_list(20)), [])

    def test_a_trailing_gap_still_counts(self):
        sessions = [session(0, 'Strength')]
        gaps = workout_blackouts(sessions, day_list(30))
        self.assertEqual(gaps[0][2], 29)


class TestInferredEvents(unittest.TestCase):
    def test_a_modality_that_stops_dead_is_surfaced(self):
        sessions = [session(i, 'Strength') for i in range(0, 60, 2)]
        events = detect_events(sessions, day_list(400))
        stops = [e for e in events if 'stops' in e.headline]
        self.assertEqual(len(stops), 1)
        self.assertIn('Strength', stops[0].headline)

    def test_a_modality_that_appears_from_nothing_is_surfaced(self):
        sessions = ([session(i, 'Strength') for i in range(0, 60, 2)]
                    + [session(i, 'Cooldown') for i in range(200, 260)])
        events = detect_events(sessions, day_list(300))
        arrivals = [e for e in events if 'appears' in e.headline]
        self.assertEqual(len(arrivals), 1)
        self.assertIn('Cooldown', arrivals[0].headline)

    def test_a_modality_present_from_the_start_is_not_called_new(self):
        sessions = [session(i, 'Walking') for i in range(0, 300, 2)]
        events = detect_events(sessions, day_list(300))
        self.assertEqual([e for e in events if 'appears' in e.headline], [])

    def test_events_never_name_a_cause(self):
        # The single most harmful inference available in this data.
        sessions = ([session(i, 'Strength') for i in range(0, 60, 2)]
                    + [session(i, 'Cooldown') for i in range(200, 260)])
        text = ' '.join(e.headline + ' '.join(e.evidence)
                        for e in detect_events(sessions, day_list(300))).lower()
        for word in ('injur', 'surgery', 'operation', 'illness', 'lazy', 'motivat'):
            self.assertNotIn(word, text)

    def test_events_are_ordered_by_date(self):
        sessions = ([session(i, 'Strength') for i in range(0, 60, 2)]
                    + [session(i, 'Cooldown') for i in range(200, 260)])
        events = detect_events(sessions, day_list(300))
        self.assertEqual([e.when for e in events], sorted(e.when for e in events))

    def test_no_workouts_means_no_events(self):
        self.assertEqual(detect_events([], day_list(300)), [])


class TestEpisodeSeverity(unittest.TestCase):
    def test_the_worst_episode_sorts_first_regardless_of_date(self):
        rows = ([{'date': '2025-01-01', 'strain_flag': 'yes', 'strain_signal_count': 2,
                  'strain_detail': 'HRV -1.0 SD'}]
                + [{'date': f'2025-06-{d:02d}', 'strain_flag': 'yes', 'strain_signal_count': 3,
                    'strain_detail': 'HRV -1.0 SD; resting HR +4.0 bpm'} for d in range(1, 8)])
        ranked = episodes_by_severity(group_strain_episodes(rows))
        self.assertEqual(ranked[0].flagged_days, 7)
        self.assertEqual(ranked[0].start, date(2025, 6, 1))


class TestSensorArtifacts(unittest.TestCase):
    def test_a_lone_implausible_spike_is_caught(self):
        rows = hist_rows(resting_hr=[64, 64, 92, 60, 61])
        found = isolated_spikes(rows)
        self.assertEqual(found, [('resting_hr', HISTORY_START + timedelta(days=2))])

    def test_a_sustained_step_change_is_not_an_artifact(self):
        # A real shift to a new level must survive; only a value both
        # neighbours contradict is a spike.
        rows = hist_rows(resting_hr=[55, 55, 55, 75, 76, 75, 76])
        self.assertEqual(isolated_spikes(rows), [])

    def test_an_edge_day_without_two_neighbours_is_left_alone(self):
        rows = hist_rows(resting_hr=[92, 60, 61])
        self.assertEqual(isolated_spikes(rows), [])

    def test_a_moderate_excursion_is_left_alone(self):
        rows = hist_rows(resting_hr=[60, 68, 61])
        self.assertEqual(isolated_spikes(rows), [])

    def test_a_flagged_artifact_cannot_raise_a_strain_signal(self):
        rows = hist_rows(resting_hr=[64] * 30 + [92] + [64] * 5,
                         hrv_sdnn=[50] * 36)
        suspect = isolated_spikes(rows)
        self.assertTrue(suspect)
        insights = build_daily_insights(rows, None, suspect)
        spike_row = next(r for r in insights if r['date'] == suspect[0][1].isoformat())
        self.assertEqual(spike_row['resting_hr_z'], '')
        self.assertEqual(spike_row['strain_flag'], '')


class TestProgressionTarget(unittest.TestCase):
    def _target(self, values):
        rows = hist_rows(exercise_minutes=values)
        history = build_history(rows, [])
        return progression_target(history.capacity)

    def test_target_is_anchored_on_recent_volume_not_on_the_record(self):
        target = self._target([100] * 28 + [10] * 28)
        self.assertAlmostEqual(target.recent_weekly, 70.0)
        self.assertAlmostEqual(target.next_week, 77.0)
        self.assertAlmostEqual(target.ceiling_weekly, 700.0)
        self.assertLess(target.next_week, target.ceiling_weekly / 5)

    def test_weeks_to_ceiling_is_reported_when_below_it(self):
        target = self._target([100] * 28 + [10] * 28)
        self.assertGreater(target.weeks_to_ceiling, 10)

    def test_no_ceiling_climb_when_already_at_the_best(self):
        target = self._target([50] * 40)
        self.assertIsNone(target.weeks_to_ceiling)

    def test_no_load_metric_means_no_target(self):
        self.assertIsNone(progression_target([]))


class TestMonthByYearGrid(unittest.TestCase):
    def test_the_same_month_in_two_years_stays_two_cells(self):
        # Pooling months across years turns a decline into a fake season.
        series = {}
        for year, value in ((2025, 60.0), (2026, 10.0)):
            for day in range(1, 29):
                series[date(year, 3, day)] = value
        days = sorted(series)
        years, grid = month_by_year_grid(series, days)
        self.assertEqual(years, [2025, 2026])
        cells = dict(grid)['Mar']
        self.assertAlmostEqual(cells[2025][0], 60.0)
        self.assertAlmostEqual(cells[2026][0], 10.0)

    def test_a_month_with_no_data_in_a_year_is_empty_not_zero(self):
        series = {date(2025, 3, d): 60.0 for d in range(1, 29)}
        days = sorted(series) + [date(2026, 3, 1)]
        _years, grid = month_by_year_grid(series, days)
        self.assertIsNone(dict(grid)['Mar'][2026])


class TestBandEdges(unittest.TestCase):
    def test_distance_to_the_nearest_band_edge(self):
        self.assertAlmostEqual(band_edge_distance(10.03), 0.03)
        self.assertAlmostEqual(band_edge_distance(45.0), 15.0)

    def test_boundary_margin_is_reported_for_every_boundary_but_the_first(self):
        rows = hist_rows(exercise_minutes=[80] * 60 + [0] * 60)
        by_date = {date.fromisoformat(r['date']): r for r in rows}
        days = day_list(120)
        series = build_metric_series(by_date, METRICS_BY_COLUMN['exercise_minutes'], days)
        eras = segment_eras(series, days, {})
        self.assertIsNone(eras[0].boundary_margin)
        self.assertIsNotNone(eras[1].boundary_margin)


class TestPercentiles(unittest.TestCase):
    def test_median_of_an_even_sample(self):
        self.assertAlmostEqual(percentile([1, 2, 3, 4], 50), 2.5)

    def test_extremes_are_the_extremes(self):
        self.assertAlmostEqual(percentile([5, 1, 9], 0), 1)
        self.assertAlmostEqual(percentile([5, 1, 9], 100), 9)

    def test_a_single_value_is_every_percentile(self):
        self.assertAlmostEqual(percentile([7], 10), 7)

    def test_empty_is_none_not_zero(self):
        self.assertIsNone(percentile([], 50))


class TestHistoryAssembly(unittest.TestCase):
    def test_empty_input_returns_an_empty_result(self):
        history = build_history([], [], None)
        self.assertEqual(history.capacity, [])
        self.assertEqual(history.n_days, 0)
        self.assertIsNone(history.first_day)

    def test_a_two_day_export_does_not_crash_or_divide_by_zero(self):
        rows = hist_rows(exercise_minutes=[10, 20], steps=[5000, 6000],
                         sleep_asleep_hours=[7.0, 8.0])
        history = build_history(rows, [], None)
        self.assertEqual(history.n_days, 2)
        self.assertTrue(history.capacity)
        self.assertTrue(all(c.windows == 0 for c in history.capacity))

    def test_gaps_in_the_export_are_counted_as_calendar_days(self):
        # Windows must feel a missing fortnight, so the day axis is the calendar,
        # not the list of rows that happen to exist.
        rows = hist_rows(steps=[5000] + [None] * 14 + [6000])
        del rows[1:15]
        history = build_history(rows, [], None)
        self.assertEqual(history.n_days, 16)
        self.assertEqual(history.days_with_rows, 2)

    def test_analysis_start_trims_earlier_days(self):
        rows = hist_rows(steps=[5000] * 10)
        history = build_history(rows, [], HISTORY_START + timedelta(days=5))
        self.assertEqual(history.first_day, HISTORY_START + timedelta(days=5))
        self.assertEqual(history.n_days, 5)

    def test_llm_context_puts_events_and_capacity_above_records(self):
        rows = hist_rows(exercise_minutes=[90] * 40 + [5] * 40,
                         steps=[12000] * 40 + [4000] * 40)
        workouts = [{'date': (HISTORY_START + timedelta(days=i)).isoformat(),
                     'activity_type': 'Strength', 'duration_min': '45'} for i in range(0, 40, 2)]
        history = build_history(rows, [], None, workouts, max_hr=180.0)
        text = render_llm_context(rows, [], [], HISTORY_START, history)
        self.assertIn('## Capacity gap', text)
        self.assertLess(text.index('## Capacity gap'), text.index('## Personal records'))
        self.assertIn('What this data cannot tell you', text)
        self.assertNotIn('None', text)

    def test_llm_context_carries_no_trend_table(self):
        # Its two periods sit inside the same stretch, so it reports a
        # sustained collapse as roughly flat. Paying tokens for a section the
        # same file then tells the reader to ignore is worse than omitting it.
        rows = hist_rows(exercise_minutes=[90] * 40 + [5] * 40)
        text = render_llm_context(rows, [], [], HISTORY_START, build_history(rows, [], None))
        self.assertNotIn('Direction of travel', text)

    def test_llm_context_never_attributes_load_to_motivation(self):
        rows = hist_rows(exercise_minutes=[90] * 40 + [5] * 40)
        history = build_history(rows, [], None)
        text = render_llm_context(rows, [], [], HISTORY_START, history).lower()
        # The word may appear only where the file forbids the inference.
        for sentence in text.split('.'):
            if 'motivation' in sentence:
                self.assertTrue('not' in sentence or 'do not' in sentence,
                                f'unguarded motivation claim: {sentence.strip()}')

    def test_workout_modality_reaches_the_pack(self):
        rows = hist_rows(exercise_minutes=[40] * 40)
        workouts = [{'date': (HISTORY_START + timedelta(days=i)).isoformat(),
                     'activity_type': 'Cooldown', 'duration_min': '40',
                     'hr_zone_z1_min': '35'} for i in range(40)]
        history = build_history(rows, [], None, workouts, max_hr=181.0)
        text = render_llm_context(rows, [], [], HISTORY_START, history)
        self.assertIn('Cooldown', text)
        self.assertIn('181 bpm', text)
        self.assertIn('| z1 |', text)


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


class TestTrainingLoadGate(unittest.TestCase):
    """ACWR is a heuristic for people training near-daily. At 10 min/day one
    session swings it across a whole category, so the label described a single
    workout rather than a pattern."""

    def _rows(self, minutes_per_day):
        rows = []
        for i in range(60):
            d = date(2026, 1, 1) + timedelta(days=i)
            rows.append({'date': d.isoformat(), 'exercise_minutes': str(minutes_per_day),
                         'wear_class': 'full'})
        return rows

    def test_low_volume_is_not_classified(self):
        insights = build_daily_insights(self._rows(10), None)
        last = insights[-1]
        self.assertEqual(last['load_status'], 'volume too low to rate')
        # The ratio itself is still shown, so the arithmetic stays auditable.
        self.assertNotEqual(last['load_ratio'], '')

    def test_adequate_volume_is_classified(self):
        insights = build_daily_insights(self._rows(45), None)
        self.assertEqual(insights[-1]['load_status'], 'steady')

    def test_threshold_is_the_who_guideline(self):
        # ~150 min/week. Below it, a single session dominates the ratio.
        self.assertAlmostEqual(ACWR_MIN_CHRONIC_LOAD, 21.0)
        self.assertLess(ACWR_MIN_CHRONIC_LOAD * 7, 155)

    def test_spike_still_detected_above_the_gate(self):
        rows = self._rows(40)
        for row in rows[-7:]:
            row['exercise_minutes'] = '120'
        insights = build_daily_insights(rows, None)
        self.assertEqual(insights[-1]['load_status'], 'spike')


class TestCyclingPower(unittest.TestCase):
    """Watts are the only measured intensity in a Health export; heart rate is
    an inferred, lagging proxy."""

    def _session(self, day, minutes, avg_w, max_w=None):
        return WorkoutSession(day=day, activity='Cycling', minutes=minutes,
                              zones={}, power_avg_w=avg_w, power_max_w=max_w)

    def test_average_is_weighted_by_duration(self):
        # A 10-minute blast must not outweigh a 90-minute ride.
        sessions = [
            self._session(date(2026, 5, 1), 90.0, 100.0),
            self._session(date(2026, 5, 2), 10.0, 300.0),
        ]
        summary = power_summary(sessions)
        self.assertAlmostEqual(summary.avg_w, 120.0, places=1)  # not the naive 200

    def test_percentage_of_ftp(self):
        sessions = [self._session(date(2026, 5, 1), 60.0, 103.0)]
        summary = power_summary(sessions, ftp_w=206.0)
        self.assertAlmostEqual(summary.pct_of_ftp, 50.0, places=1)

    def test_no_ftp_leaves_percentage_unset(self):
        sessions = [self._session(date(2026, 5, 1), 60.0, 110.0)]
        self.assertIsNone(power_summary(sessions).pct_of_ftp)

    def test_sessions_without_power_are_ignored(self):
        sessions = [
            self._session(date(2026, 5, 1), 60.0, 110.0),
            WorkoutSession(day=date(2026, 5, 2), activity='Walking', minutes=60.0, zones={}),
        ]
        self.assertEqual(power_summary(sessions).sessions, 1)

    def test_no_power_data_returns_none(self):
        sessions = [WorkoutSession(day=date(2026, 5, 2), activity='Walking',
                                   minutes=60.0, zones={})]
        self.assertIsNone(power_summary(sessions))

    def test_window_scoping(self):
        sessions = [
            self._session(date(2026, 4, 1), 60.0, 100.0),
            self._session(date(2026, 5, 1), 60.0, 200.0),
        ]
        scoped = power_summary(sessions, None, date(2026, 4, 20), date(2026, 5, 30))
        self.assertEqual(scoped.sessions, 1)
        self.assertAlmostEqual(scoped.avg_w, 200.0)

    def test_best_ride_is_reported_with_its_date(self):
        sessions = [
            self._session(date(2026, 5, 1), 60.0, 100.0),
            self._session(date(2026, 5, 8), 60.0, 125.0, max_w=610.0),
        ]
        summary = power_summary(sessions)
        self.assertAlmostEqual(summary.best_avg_w, 125.0)
        self.assertEqual(summary.best_day, date(2026, 5, 8))
        self.assertAlmostEqual(summary.max_w, 610.0)


class TestMcpOrdinals(unittest.TestCase):
    """Percentiles are user-facing text; "43th" reads as broken."""

    def test_common_suffixes(self):
        self.assertEqual(ordinal(1), '1st')
        self.assertEqual(ordinal(2), '2nd')
        self.assertEqual(ordinal(3), '3rd')
        self.assertEqual(ordinal(43), '43rd')

    def test_teens_are_the_exception(self):
        # A naive last-digit rule produces 11st, 12nd, 13rd.
        self.assertEqual(ordinal(11), '11th')
        self.assertEqual(ordinal(12), '12th')
        self.assertEqual(ordinal(13), '13th')
        self.assertEqual(ordinal(113), '113th')

    def test_hundreds_wrap_correctly(self):
        self.assertEqual(ordinal(101), '101st')
        self.assertEqual(ordinal(0), '0th')


class TestMcpProtocol(unittest.TestCase):
    """The JSON-RPC layer is hand-rolled, so a protocol regression would be
    invisible to every other test in this file."""

    def setUp(self):
        self.data = HealthData(tempfile.mkdtemp())

    def test_initialize_echoes_a_supported_protocol(self):
        reply = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {'protocolVersion': '2024-11-05'}}, self.data)
        self.assertEqual(reply['result']['protocolVersion'], '2024-11-05')

    def test_unknown_protocol_falls_back_rather_than_failing(self):
        reply = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {'protocolVersion': '1999-01-01'}}, self.data)
        self.assertEqual(reply['result']['protocolVersion'], DEFAULT_PROTOCOL)

    def test_notifications_get_no_reply(self):
        # Answering a notification is a protocol violation; some clients hang up.
        self.assertIsNone(handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                 self.data))

    def test_every_tool_is_listed_with_a_schema(self):
        reply = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, self.data)
        tools = reply['result']['tools']
        self.assertEqual(len(tools), len(HANDLERS))
        for t in tools:
            self.assertTrue(t['description'].strip(), f"{t['name']} has no description")
            self.assertEqual(t['inputSchema']['type'], 'object')

    def test_declared_required_args_exist_in_properties(self):
        for t in TOOLS:
            schema = t['inputSchema']
            for name in schema.get('required', []):
                self.assertIn(name, schema['properties'],
                              f"{t['name']} requires '{name}' but never declares it")

    def test_unknown_method_is_a_protocol_error(self):
        reply = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'nope/nope'}, self.data)
        self.assertEqual(reply['error']['code'], -32601)

    def test_unknown_tool_is_reported(self):
        reply = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'health_nope', 'arguments': {}}}, self.data)
        self.assertEqual(reply['error']['code'], -32602)

    def test_missing_data_explains_itself_instead_of_crashing(self):
        reply = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'health_overview', 'arguments': {}}}, self.data)
        self.assertTrue(reply['result']['isError'])
        self.assertIn('convert_health_data.py', reply['result']['content'][0]['text'])

    def test_ping(self):
        self.assertEqual(handle({'jsonrpc': '2.0', 'id': 9, 'method': 'ping'},
                                self.data)['result'], {})


class TestMcpDataLayer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, 'daily_metrics.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['date', 'steps', 'sleep_asleep_hours', 'wear_class'])
            w.writerow(['2026-01-01', '5000', '7.0', 'full'])
            w.writerow(['2026-01-02', '', '6.5', 'full'])       # blank, not zero
            w.writerow(['2026-01-03', '9000', '', 'partial'])
        self.data = HealthData(self.dir)

    def test_blank_cells_are_absent_not_zero(self):
        series = self.data.series('steps')
        self.assertEqual(len(series), 2)
        self.assertNotIn(date(2026, 1, 2), series)

    def test_date_range_filter_is_inclusive(self):
        series = self.data.series('steps', date(2026, 1, 3), date(2026, 1, 3))
        self.assertEqual(list(series), [date(2026, 1, 3)])

    def test_stats_reports_measured_days_only(self):
        text = _metric_stats(self.data, {'metric': 'steps'})
        self.assertIn('n: **2**', text)

    def test_unknown_metric_points_at_the_discovery_tool(self):
        text = _metric_stats(self.data, {'metric': 'not_a_metric'})
        self.assertIn('health_list_metrics', text)

    def test_bad_date_raises_a_clear_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _metric_stats(self.data, {'metric': 'steps', 'start': 'yesterday'})
        self.assertIn('YYYY-MM-DD', str(ctx.exception))

    def test_thin_comparison_is_flagged(self):
        text = _compare(self.data, {'metric': 'steps', 'a_start': '2026-01-01',
                                    'a_end': '2026-01-01', 'b_start': '2026-01-03',
                                    'b_end': '2026-01-03'})
        self.assertIn('thin ground', text)

    def test_client_config_is_valid_json_with_absolute_paths(self):
        cfg = json.loads(client_config(self.dir))
        server = cfg['mcpServers']['apple-health']
        self.assertTrue(os.path.isabs(server['command']))
        self.assertIn('--data-dir', server['args'])
        self.assertTrue(all(os.path.isabs(a) for a in server['args'] if a != '--data-dir'))


class TestMcpOutputLimits(unittest.TestCase):
    """A local model with an 8K window is the constraint here: one uncapped
    tool result can consume the whole context before it answers anything."""

    def setUp(self):
        self._original = health_mcp.MAX_OUTPUT_CHARS

    def tearDown(self):
        health_mcp.MAX_OUTPUT_CHARS = self._original

    def test_short_text_is_untouched(self):
        health_mcp.MAX_OUTPUT_CHARS = 1000
        self.assertEqual(health_mcp.truncate('short'), 'short')

    def test_long_text_is_cut_and_announced(self):
        health_mcp.MAX_OUTPUT_CHARS = 200
        out = health_mcp.truncate('x' * 5000)
        self.assertLess(len(out), 1000)
        self.assertIn('truncated', out)

    def test_truncation_warns_the_model_not_to_trust_the_last_row(self):
        # Silently dropping rows is the dangerous case: a truncated table is
        # indistinguishable from a complete one.
        health_mcp.MAX_OUTPUT_CHARS = 200
        out = health_mcp.truncate('row\n' * 500)
        self.assertIn('INCOMPLETE', out)

    def test_cut_lands_on_a_line_boundary(self):
        health_mcp.MAX_OUTPUT_CHARS = 300
        out = health_mcp.truncate('\n'.join(f'line {i}' for i in range(200)))
        body = out.split('_[truncated')[0]
        self.assertFalse(body.rstrip('\n').endswith('lin'))


class TestMcpSections(unittest.TestCase):
    DOC = """# Pack

preamble text

## Situation

body one

## Capacity gap

body two

## Limits

body three
"""

    def test_sections_are_split_on_top_level_headings(self):
        sections = health_mcp.markdown_sections(self.DOC)
        self.assertEqual(list(sections), ['Situation', 'Capacity gap', 'Limits'])

    def test_section_body_includes_its_heading(self):
        sections = health_mcp.markdown_sections(self.DOC)
        self.assertTrue(sections['Situation'].startswith('## Situation'))
        self.assertIn('body one', sections['Situation'])

    def test_preamble_before_the_first_heading_is_not_a_section(self):
        self.assertNotIn('preamble text', ''.join(health_mcp.markdown_sections(self.DOC).values()))

    def test_empty_document_yields_nothing(self):
        self.assertEqual(health_mcp.markdown_sections(''), {})


class TestExportIngest(unittest.TestCase):
    """Whatever the user points at should work: the zip from their phone, the
    unzipped folder, or the folder they dropped it in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = tempfile.mkdtemp()

    def _make_zip(self, path, members=None):
        members = members or {'apple_health_export/export.xml': '<HealthData/>'}
        with zipfile.ZipFile(path, 'w') as z:
            for name, body in members.items():
                z.writestr(name, body)
        return path

    def test_plain_folder_with_export_is_used_directly(self):
        open(os.path.join(self.tmp, 'export.xml'), 'w').close()
        found = health_ingest.resolve(self.tmp, self.out)
        self.assertEqual(found.directory, self.tmp)
        self.assertFalse(found.extracted)

    def test_parent_of_apple_health_export_descends(self):
        nested = os.path.join(self.tmp, 'apple_health_export')
        os.makedirs(nested)
        open(os.path.join(nested, 'export.xml'), 'w').close()
        found = health_ingest.resolve(self.tmp, self.out)
        self.assertEqual(found.directory, nested)

    def test_zip_is_extracted(self):
        z = self._make_zip(os.path.join(self.tmp, 'export.zip'))
        found = health_ingest.resolve(z, self.out)
        self.assertTrue(found.extracted)
        self.assertTrue(os.path.exists(os.path.join(found.directory, 'export.xml')))

    def test_folder_containing_a_zip_picks_it_up(self):
        self._make_zip(os.path.join(self.tmp, 'export.zip'))
        found = health_ingest.resolve(self.tmp, self.out)
        self.assertTrue(found.extracted)

    def test_newest_zip_wins(self):
        old = self._make_zip(os.path.join(self.tmp, 'old.zip'),
                             {'apple_health_export/export.xml': 'OLD'})
        new = self._make_zip(os.path.join(self.tmp, 'new.zip'),
                             {'apple_health_export/export.xml': 'NEW'})
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))
        found = health_ingest.resolve(self.tmp, self.out)
        with open(os.path.join(found.directory, 'export.xml')) as f:
            self.assertEqual(f.read(), 'NEW')

    def test_extraction_is_cached_between_runs(self):
        z = self._make_zip(os.path.join(self.tmp, 'export.zip'))
        first = health_ingest.resolve(z, self.out).directory
        marker = os.path.join(first, 'export.xml')
        stamp = os.path.getmtime(marker)
        health_ingest.resolve(z, self.out)
        self.assertEqual(os.path.getmtime(marker), stamp)

    def test_a_newer_zip_invalidates_the_cache(self):
        z = os.path.join(self.tmp, 'export.zip')
        self._make_zip(z, {'apple_health_export/export.xml': 'FIRST'})
        health_ingest.resolve(z, self.out)
        self._make_zip(z, {'apple_health_export/export.xml': 'SECOND'})
        os.utime(z, None)
        found = health_ingest.resolve(z, self.out)
        with open(os.path.join(found.directory, 'export.xml')) as f:
            self.assertEqual(f.read(), 'SECOND')

    def test_zip_slip_members_are_refused(self):
        # A zip can name ../ or absolute paths; extracting those writes outside
        # the destination. The archive is the user's own, but a tool other
        # people run should not assume that.
        z = self._make_zip(os.path.join(self.tmp, 'export.zip'), {
            'apple_health_export/export.xml': 'ok',
            'apple_health_export/../../escaped.txt': 'nope',
        })
        found = health_ingest.resolve(z, self.out)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.out), 'escaped.txt')))
        self.assertTrue(os.path.exists(os.path.join(found.directory, 'export.xml')))

    def test_only_needed_members_are_extracted(self):
        # A full export is ~1.4 GB unpacked; route GPX and the CDA mirror are
        # never read, so unpacking them is pure cost.
        z = self._make_zip(os.path.join(self.tmp, 'export.zip'), {
            'apple_health_export/export.xml': 'ok',
            'apple_health_export/export_cda.xml': 'big',
            'apple_health_export/workout-routes/route.gpx': 'big',
            'apple_health_export/electrocardiograms/ecg_1.csv': 'small',
        })
        found = health_ingest.resolve(z, self.out)
        self.assertTrue(os.path.exists(os.path.join(found.directory, 'export.xml')))
        self.assertTrue(os.path.exists(os.path.join(found.directory, 'electrocardiograms', 'ecg_1.csv')))
        self.assertFalse(os.path.exists(os.path.join(found.directory, 'export_cda.xml')))
        self.assertFalse(os.path.exists(os.path.join(found.directory, 'workout-routes')))

    def test_zip_without_an_export_is_rejected_clearly(self):
        z = self._make_zip(os.path.join(self.tmp, 'export.zip'), {'notes.txt': 'hi'})
        with self.assertRaises(ValueError) as ctx:
            health_ingest.resolve(z, self.out)
        self.assertIn('Apple Health export', str(ctx.exception))

    def test_empty_folder_explains_what_to_do(self):
        with self.assertRaises(ValueError) as ctx:
            health_ingest.resolve(self.tmp, self.out)
        self.assertIn('Export All Health Data', str(ctx.exception))

    def test_missing_path_is_reported(self):
        with self.assertRaises(ValueError):
            health_ingest.resolve(os.path.join(self.tmp, 'nope'), self.out)


class TestScheduleGeneration(unittest.TestCase):
    """The scheduled job is pasted straight into launchd/systemd/schtasks, so a
    malformed one fails at install time with an opaque error."""

    ARGS = {'python': '/usr/bin/python3', 'script': '/opt/app/convert_health_data.py',
            'watch': '/Users/me/Downloads', 'out': '/Users/me/health'}

    def test_macos_emits_a_parseable_plist(self):
        text = health_ingest.schedule_instructions(**self.ARGS, platform_name='darwin')
        parsed = plistlib.loads(text[text.index('<?xml'):].encode())
        self.assertEqual(parsed['Label'], 'com.health-context.rebuild')
        self.assertEqual(parsed['StartCalendarInterval'], {'Day': 1, 'Hour': 9})

    def test_macos_plist_carries_the_real_paths(self):
        text = health_ingest.schedule_instructions(**self.ARGS, platform_name='darwin')
        argv = plistlib.loads(text[text.index('<?xml'):].encode())['ProgramArguments']
        self.assertEqual(argv[0], self.ARGS['python'])
        self.assertIn(self.ARGS['watch'], argv)
        self.assertIn(self.ARGS['out'], argv)

    def test_linux_emits_both_unit_files(self):
        text = health_ingest.schedule_instructions(**self.ARGS, platform_name='linux')
        self.assertIn('[Service]', text)
        self.assertIn('[Timer]', text)
        self.assertIn('OnCalendar=monthly', text)
        self.assertIn(self.ARGS['script'], text)

    def test_windows_emits_a_schtasks_command(self):
        text = health_ingest.schedule_instructions(**self.ARGS, platform_name='win32')
        self.assertIn('schtasks /Create', text)
        self.assertIn('/SC MONTHLY', text)

    def test_unknown_platform_falls_back_to_systemd(self):
        text = health_ingest.schedule_instructions(**self.ARGS, platform_name='freebsd99')
        self.assertIn('[Timer]', text)


class TestProfile(unittest.TestCase):
    """The export records what a body did and can never record why. Without a
    profile every reader fills that gap with a guess, and the usual guess is
    that a decline means lost motivation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, text, name='profile.md'):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_an_unedited_template_counts_as_no_profile(self):
        # A profile that is only the template's own prompts looks like context
        # while saying nothing, which is worse than admitting there is none.
        with open('profile.example.md', encoding='utf-8') as f:
            template = f.read()
        self.assertEqual(health_ingest.load_profile(self._write(template)), '')

    def test_a_real_profile_is_returned(self):
        path = self._write('# Profile\n\n## Constraints\n- Wrist surgery Nov 2025\n')
        self.assertIn('Wrist surgery', health_ingest.load_profile(path))

    def test_template_comments_are_stripped(self):
        path = self._write('## Goals\n<!-- be specific, e.g. rebuild strength -->\n- Run 10k\n')
        loaded = health_ingest.load_profile(path)
        self.assertIn('Run 10k', loaded)
        self.assertNotIn('be specific', loaded)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(health_ingest.load_profile(None), '')
        self.assertEqual(health_ingest.load_profile('/nonexistent/profile.md'), '')

    def test_find_profile_prefers_the_first_directory(self):
        other = tempfile.mkdtemp()
        with open(os.path.join(other, 'profile.md'), 'w') as f:
            f.write('second')
        self._write('first')
        self.assertEqual(health_ingest.find_profile(self.tmp, other),
                         os.path.join(self.tmp, 'profile.md'))

    def test_find_profile_returns_none_when_absent(self):
        self.assertIsNone(health_ingest.find_profile(self.tmp))

    def test_pack_states_plainly_when_no_profile_exists(self):
        text = render_llm_context([], [], [], None, profile='')
        self.assertIn('No profile provided', text)
        self.assertIn('do not read a decline in training as lost motivation', text)

    def test_pack_tells_the_reader_to_believe_the_profile_over_inference(self):
        text = render_llm_context([], [], [], None, profile='- Wrist surgery Nov 2025')
        self.assertIn('Wrist surgery', text)
        self.assertIn('believe this', text)

    def test_profile_precedes_every_metric_section(self):
        # It has to be read before the numbers, or it cannot reframe them.
        text = render_llm_context([], [], [], None, profile='- Recovering from surgery')
        # Match the heading, not the preamble's reference to it by name.
        self.assertLess(text.index('## Who this is'),
                        text.index('## What this data cannot tell you'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
