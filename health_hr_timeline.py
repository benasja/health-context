"""Heart rate through the day, in five-minute buckets, with what was going on.

The daily rows keep one resting and one average heart rate per day, and the
workout series keeps every minute of every workout. Neither can show a day:
the climb on the way to work, the evening that never settled, the night whose
heart rate only dropped after 3 a.m. This keeps every reading the Watch took,
folded into five-minute buckets, and labels each bucket with the workout or
sleep stage it fell in, so a graph of a day reads without cross-referencing.

What the labels can't say: the phone sends steps only as daily totals, so a
bucket outside a workout or sleep is left unlabelled rather than guessed as
walking or resting.
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
FIELDS = ['time', 'avg_hr', 'min_hr', 'max_hr', 'samples', 'context', 'source']

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


def build_hr_timeline(data: ExportData) -> list[dict]:
    buckets: dict[datetime, list[float]] = defaultdict(list)
    sources: dict[datetime, Counter] = defaultdict(Counter)
    for sample in heart_rate_samples(data):
        start = sample.start
        bucket = start.replace(minute=start.minute - start.minute % BUCKET_MINUTES, second=0, microsecond=0)
        buckets[bucket].append(sample.value)
        sources[bucket][normalize_source(sample.source_name)] += 1

    workouts, sleep = _contexts(data)
    half = timedelta(minutes=BUCKET_MINUTES / 2)
    rows = []
    for bucket in sorted(buckets):
        values = buckets[bucket]
        middle = bucket + half
        rows.append({
            'time': bucket.strftime(TIME_FORMAT),
            'avg_hr': round(statistics.mean(values), 1),
            'min_hr': round(min(values)),
            'max_hr': round(max(values)),
            'samples': len(values),
            'context': workouts.label_at(middle) or sleep.label_at(middle),
            'source': sources[bucket].most_common(1)[0][0],
        })
    return rows


def write_hr_timeline(out_dir: str, data: ExportData) -> dict[str, str]:
    path = os.path.join(out_dir, 'hr_timeline.csv')
    write_csv(path, FIELDS, build_hr_timeline(data))
    return {'hr_timeline': path}
