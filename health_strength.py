"""Strength training logged in Wariden, set by set.

Apple Health keeps a strength workout as a block of time with heart rate and
energy; what was lifted exists only in the app it was logged in. Wariden sends
every ticked set to the Health Bridge receiver as a `strength_set` sample --
the weight in kg as the value, everything else in the sample's metadata -- and
this module turns those samples into two plain CSVs next to the rest of the
outputs:

* ``strength_sets.csv`` -- one row per set: the portable log. It opens in any
  spreadsheet and imports into anything that reads CSV.
* ``strength_sessions.csv`` -- one row per workout, with the Apple Health
  workout recorded over the same time (energy, heart rate) alongside, so the
  lifting and what it cost sit in one row.

A set's estimated one-rep max uses Epley's formula, and only where it means
something: a working set with weight on the bar and at most 12 reps. Past
that, or for bodyweight work, it is left blank rather than guessed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any

from health_coaching import local_calendar_date, write_csv

STRENGTH_SET_TYPE = 'strength_set'
E1RM_MAX_REPS = 12
TIME_FORMAT = '%Y-%m-%d %H:%M:%S %z'

SET_FIELDS = [
    'date', 'workout_id', 'workout_start', 'program', 'exercise_number', 'exercise',
    'equipment', 'muscle_group', 'set_number', 'set_type', 'warmup', 'weight_kg', 'reps',
    'rpe', 'volume_kg', 'e1rm_kg', 'time', 'notes',
]
SESSION_FIELDS = [
    'workout_id', 'date', 'start_datetime', 'end_datetime', 'duration_min', 'program',
    'exercises', 'working_sets', 'warmup_sets', 'total_reps', 'volume_kg', 'exercise_summary',
    'active_energy_kcal', 'avg_heart_rate_bpm', 'max_heart_rate_bpm', 'health_activity_type',
    'health_workout_id', 'notes',
]


@dataclass
class StrengthSet:
    workout_id: str
    workout_start: datetime
    workout_end: datetime
    program: str
    exercise: str
    equipment: str
    muscle_group: str
    exercise_number: int
    set_number: int
    set_type: str
    warmup: bool
    weight_kg: float
    reps: int
    rpe: int | None
    time: datetime
    notes: str
    workout_notes: str


def _utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _int(raw: Any) -> int | None:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def strength_set(meta: dict, weight_kg: float, time: datetime, tz: tzinfo) -> StrengthSet | None:
    """One `strength_set` sample, or None when it lacks what makes it a set."""
    workout_id = str(meta.get('workout_id') or '')
    reps = _int(meta.get('reps'))
    if not workout_id or reps is None or not meta.get('exercise'):
        return None
    local = time.astimezone(tz)
    try:
        start = _utc(meta['workout_start']).astimezone(tz)
    except (KeyError, ValueError):
        start = local
    try:
        end = _utc(meta['workout_end']).astimezone(tz)
    except (KeyError, ValueError):
        end = start
    return StrengthSet(
        workout_id=workout_id,
        workout_start=start,
        workout_end=max(start, end),
        program=str(meta.get('program') or ''),
        exercise=str(meta['exercise']),
        equipment=str(meta.get('equipment') or ''),
        muscle_group=str(meta.get('muscle_group') or ''),
        exercise_number=_int(meta.get('exercise_number')) or 0,
        set_number=_int(meta.get('set_number')) or 0,
        set_type=str(meta.get('set_type') or ''),
        warmup=str(meta.get('warmup')).lower() == 'true',
        weight_kg=float(weight_kg),
        reps=reps,
        rpe=_int(meta.get('rpe')),
        time=local,
        notes=str(meta.get('notes') or ''),
        workout_notes=str(meta.get('workout_notes') or ''),
    )


def is_warmup(s: StrengthSet) -> bool:
    return s.warmup or s.set_type.lower() == 'warmup'


def e1rm(s: StrengthSet) -> float | None:
    """Epley's estimate, for a working set it fits; None otherwise."""
    if is_warmup(s) or s.weight_kg <= 0 or not 1 <= s.reps <= E1RM_MAX_REPS:
        return None
    return s.weight_kg if s.reps == 1 else s.weight_kg * (1 + s.reps / 30)


def _kg(value: float) -> str:
    return f'{value:g}'


def describe_set(s: StrengthSet) -> str:
    """'22.5x8', or 'BWx8' for bodyweight."""
    return f"{_kg(s.weight_kg) if s.weight_kg > 0 else 'BW'}x{s.reps}"


def _ordered(sets: list[StrengthSet]) -> list[StrengthSet]:
    return sorted(sets, key=lambda s: (s.workout_start, s.workout_id, s.exercise_number, s.set_number))


def build_set_rows(sets: list[StrengthSet]) -> list[dict[str, Any]]:
    rows = []
    for s in _ordered(sets):
        estimate = e1rm(s)
        rows.append({
            'date': local_calendar_date(s.workout_start).isoformat(),
            'workout_id': s.workout_id,
            'workout_start': s.workout_start.strftime(TIME_FORMAT),
            'program': s.program,
            'exercise_number': s.exercise_number,
            'exercise': s.exercise,
            'equipment': s.equipment,
            'muscle_group': s.muscle_group,
            'set_number': s.set_number,
            'set_type': s.set_type,
            'warmup': 'yes' if is_warmup(s) else '',
            'weight_kg': _kg(s.weight_kg),
            'reps': s.reps,
            'rpe': s.rpe if s.rpe is not None else '',
            'volume_kg': '' if is_warmup(s) else _kg(round(s.weight_kg * s.reps, 1)),
            'e1rm_kg': round(estimate, 1) if estimate is not None else '',
            'time': s.time.strftime(TIME_FORMAT),
            'notes': s.notes,
        })
    return rows


def _health_workout(start: datetime, end: datetime,
                    workout_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The Apple Health workout overlapping this session the most, if any does."""
    best, best_overlap = None, 0.0
    for row in workout_rows:
        try:
            w_start = datetime.strptime(str(row['start_datetime']), TIME_FORMAT)
            w_end = datetime.strptime(str(row['end_datetime']), TIME_FORMAT)
        except (KeyError, ValueError):
            continue
        overlap = (min(end, w_end) - max(start, w_start)).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = row, overlap
    return best


def build_session_rows(sets: list[StrengthSet],
                       workout_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_workout: dict[str, list[StrengthSet]] = {}
    for s in _ordered(sets):
        by_workout.setdefault(s.workout_id, []).append(s)

    rows = []
    for workout_id, session in by_workout.items():
        first = session[0]
        working = [s for s in session if not is_warmup(s)]
        exercises: dict[tuple[int, str], list[StrengthSet]] = {}
        for s in working:
            exercises.setdefault((s.exercise_number, s.exercise), []).append(s)
        summary = '; '.join(f"{name} {', '.join(describe_set(s) for s in done)}"
                            for (_n, name), done in exercises.items())
        # Wariden's own end, when the workout was finished; else the last set.
        end = first.workout_end if first.workout_end > first.workout_start else session[-1].time
        matched = _health_workout(first.workout_start, end, workout_rows) or {}
        rows.append({
            'workout_id': workout_id,
            'date': local_calendar_date(first.workout_start).isoformat(),
            'start_datetime': first.workout_start.strftime(TIME_FORMAT),
            'end_datetime': end.strftime(TIME_FORMAT),
            'duration_min': round((end - first.workout_start).total_seconds() / 60, 1),
            'program': first.program,
            'exercises': len({s.exercise_number for s in session}),
            'working_sets': len(working),
            'warmup_sets': len(session) - len(working),
            'total_reps': sum(s.reps for s in working),
            'volume_kg': _kg(round(sum(s.weight_kg * s.reps for s in working), 1)),
            'exercise_summary': summary,
            'active_energy_kcal': matched.get('active_energy_kcal', ''),
            'avg_heart_rate_bpm': matched.get('avg_heart_rate_bpm', ''),
            'max_heart_rate_bpm': matched.get('max_heart_rate_bpm', ''),
            'health_activity_type': matched.get('activity_type', ''),
            'health_workout_id': matched.get('workout_id', ''),
            'notes': first.workout_notes,
        })
    return rows


def write_strength_outputs(out_dir: str, sets: list[StrengthSet],
                           workout_rows: list[dict[str, Any]]) -> dict[str, str]:
    """Both files, written even when empty so a reader can tell "none logged" from "not built"."""
    paths = {
        'strength_sets': os.path.join(out_dir, 'strength_sets.csv'),
        'strength_sessions': os.path.join(out_dir, 'strength_sessions.csv'),
    }
    write_csv(paths['strength_sets'], SET_FIELDS, build_set_rows(sets))
    write_csv(paths['strength_sessions'], SESSION_FIELDS, build_session_rows(sets, workout_rows))
    return paths
