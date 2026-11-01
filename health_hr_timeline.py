"""Heart rate through the day, in five-minute buckets, with what was going on.

The daily rows keep one resting and one average heart rate per day, and the
workout series keeps every minute of every workout. Neither can show a day:
the climb on the way to work, the evening that never settled, the night whose
heart rate only dropped after 3 a.m. This keeps every reading the Watch took,
folded into five-minute buckets, and labels each bucket with the workout or
sleep stage it fell in, so a graph of a day reads without cross-referencing.

Outside workouts and sleep, the label comes from the steps taken in the same
five minutes, which Wariden sends to the receiver per five-minute bucket:
walking, light activity or stationary. Where there is no five-minute step data
(an export.xml, or days before the phone started sending it) the bucket is left
unlabelled rather than guessed.
"""

from __future__ import annotations

import os
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from health_coaching import (
    SLEEP_AWAKE,
    SLEEP_CORE,
    SLEEP_DEEP,
    SLEEP_IN_BED,
    SLEEP_REM,
    ExportData,
    chosen_sleep_sessions,
    friendly_workout_type,
    heart_rate_samples,
    normalize_source,
    write_csv,
)

BUCKET_MINUTES = 5
TIME_FORMAT = '%Y-%m-%d %H:%M %z'
FIELDS = ['time', 'avg_hr', 'min_hr', 'max_hr', 'samples', 'steps', 'context', 'source']

# The receiver's type code for Wariden's five-minute step counts.
STEP_BUCKET_TYPE = 'steps_5min'
# Steps in five minutes from which a bucket counts as walking: 60 a minute on
# average. Below about 60 steps a minute stepping is incidental -- moving about
# a room -- and from 100 a minute it is a moderate walk (Tudor-Locke et al.,
# 2018), so 300 is a steady walk across the bucket or a brisk one filling three
# of its five minutes. A walk that only clips a bucket's edge reads as light
# activity rather than claiming the whole bucket.
WALKING_STEPS = 300
# Fewer than this (four a minute) is sitting or standing still: the Watch counts
# a few steps from arm movement alone, and a step or two to reach something is
# not activity that moves heart rate.
STATIONARY_STEPS = 20
STEP_LABELS = ('walking', 'light activity', 'stationary')

STAGE_LABELS = {
    SLEEP_DEEP: 'sleep: deep',
    SLEEP_CORE: 'sleep: core',
    SLEEP_REM: 'sleep: rem',
    SLEEP_AWAKE: 'sleep: awake',
    SLEEP_IN_BED: 'sleep: in bed',
}


class _Intervals:
    """Labelled time spans, looked up by the moment inside them."""

    def __init__(self, spans: list[tuple[datetime, datetime, str]]):
        self.spans = sorted(spans, key=lambda s: s[0])
        self.starts = [s[0] for s in self.spans]

    def label_at(self, moment: datetime) -> str:
        i = bisect_right(self.starts, moment)
        # Spans can overlap (a workout inside another app's longer one), so a
        # few earlier starts are checked too; the latest-starting match wins.
        for start, end, label in reversed(self.spans[max(0, i - 4):i]):
            if start <= moment < end:
                return label
        return ''


def _contexts(data: ExportData) -> tuple[_Intervals, _Intervals]:
    workouts = _Intervals([(w.start, w.end, f'workout: {friendly_workout_type(w.workout_activity_type)}')
                           for w in data.workouts])
    sleep = []
    for _source, session in chosen_sleep_sessions(data.records).values():
        for start, end, stage in session:
            sleep.append((start, end, STAGE_LABELS.get(stage, 'sleep: asleep')))
    return workouts, _Intervals(sleep)


def _bucket_start(moment: datetime) -> datetime:
    return moment.replace(minute=moment.minute - moment.minute % BUCKET_MINUTES, second=0, microsecond=0)


def add_step_bucket(data: ExportData, start: datetime, steps: float) -> bool:
    """Keep one five-minute step count, `start` in local time. False if unusable.

    Two counts for one bucket (a second phone) keep the larger, as HealthKit's
    own merge of devices would rather than adding them up.
    """
    if not steps >= 0:   # also catches NaN
        return False
    bucket = _bucket_start(start)
    data.step_buckets[bucket] = max(steps, data.step_buckets.get(bucket, 0.0))
    return True


def step_context(steps: float | None) -> str:
    """What the steps in a five-minute bucket say was going on; '' if unknown."""
    if steps is None:
        return ''
    if steps >= WALKING_STEPS:
        return 'walking'
    if steps < STATIONARY_STEPS:
        return 'stationary'
    return 'light activity'


def build_hr_timeline(data: ExportData) -> list[dict]:
    """One row per five-minute bucket with a heart-rate reading or a step.

    A bucket with steps but no reading still gets a row (its heart-rate columns
    empty), so the steps of an hour or a day add up over the rows. Steps are
    known on any local day that has five-minute step data at all: the phone
    sends only buckets with steps, so on such a day a bucket it did not send had
    none. On other days they are left empty -- unknown, not zero.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    sources: dict[datetime, Counter] = defaultdict(Counter)
    for sample in heart_rate_samples(data):
        bucket = _bucket_start(sample.start)
        buckets[bucket].append(sample.value)
        sources[bucket][normalize_source(sample.source_name)] += 1
    step_days = {bucket.date() for bucket in data.step_buckets}
    for bucket, steps in data.step_buckets.items():
        # Aware datetimes compare by instant, so this finds a heart-rate bucket
        # written with a different zone object for the same moment.
        if round(steps) >= 1 and bucket not in buckets:
            buckets[bucket] = []

    workouts, sleep = _contexts(data)
    half = timedelta(minutes=BUCKET_MINUTES / 2)
    rows = []
    for bucket in sorted(buckets):
        values = buckets[bucket]
        middle = bucket + half
        steps = data.step_buckets.get(bucket, 0.0) if bucket.date() in step_days else None
        rows.append({
            'time': bucket.strftime(TIME_FORMAT),
            'avg_hr': round(statistics.mean(values), 1) if values else '',
            'min_hr': round(min(values)) if values else '',
            'max_hr': round(max(values)) if values else '',
            'samples': len(values),
            'steps': '' if steps is None else round(steps),
            'context': workouts.label_at(middle) or sleep.label_at(middle) or step_context(steps),
            'source': sources[bucket].most_common(1)[0][0] if values else '',
        })
    return rows


def write_hr_timeline(out_dir: str, data: ExportData) -> dict[str, str]:
    path = os.path.join(out_dir, 'hr_timeline.csv')
    write_csv(path, FIELDS, build_hr_timeline(data))
    return {'hr_timeline': path}
