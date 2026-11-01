"""Suggestions made from the data, kept so they can be checked against it later.

A conversation ends and its advice goes with it: "get to bed by 23:30" is
given, maybe followed, and never looked at again. This keeps each suggestion
with the evidence it was made on and the metrics that should move if it
works, in which direction. Later, the days since it started are compared with
the days before, so the next conversation can say whether it did anything.

The log is an append-only JSON Lines file of events: a suggestion is added
once, and every later change (being followed, dropped, a note) is a new line.
The current state is replayed from the events, so nothing is ever overwritten
and the history of a suggestion stays readable. It lives outside the rebuilt
outputs, which are replaced on every rebuild.

What the comparison can't say: one person, no control, and whatever else
changed at the same time. "Better" means the change is larger than the
day-to-day noise of the weeks before, not that the suggestion caused it.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

LOG_NAME = 'recommendations.jsonl'
STATUSES = ['suggested', 'following', 'not following', 'ended']
OPEN_STATUSES = {'suggested', 'following'}
DEFAULT_REVIEW_DAYS = 14
DEFAULT_BASELINE_DAYS = 28
MIN_AFTER_DAYS = 5
MIN_BASELINE_DAYS = 7
# A change counts only when it is both clear of the noise (two standard errors)
# and big enough to matter (a quarter of a typical day-to-day swing).
NOISE_SE = 2.0
MIN_EFFECT_SD = 0.25


@dataclass
class Recommendation:
    id: str
    added: str
    start: date
    text: str
    why: str
    judge_by: list[dict[str, str]]
    review_days: int
    baseline_days: int
    status: str = 'suggested'
    notes: list[tuple[str, str, str]] = field(default_factory=list)  # (when, status, note)

    @property
    def review_on(self) -> date:
        return self.start + timedelta(days=self.review_days)

    def is_due(self, today: date) -> bool:
        return self.status in OPEN_STATUSES and today >= self.review_on


def state_dir_for(data_dir: str) -> str:
    """Where the log lives by default.

    health_sync.py serves data_dir as a link inside a folder that also holds
    the snapshots it rotates; the log goes in that folder, beside them. A
    folder written by convert_health_data.py is kept as it is, so the log goes
    in it.
    """
    root = os.path.abspath(data_dir)
    parent = os.path.dirname(root)
    return parent if os.path.isdir(os.path.join(parent, 'snapshots')) else root


def _event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None  # a torn last line from a crash mid-write; the rest still stands
    return event if isinstance(event, dict) else None


class RecommendationLog:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, LOG_NAME)

    def _events(self) -> list[dict]:
        try:
            with open(self.path, encoding='utf-8') as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        return [e for e in map(_event, lines) if e is not None]

    def _append(self, event: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    def all(self) -> list[Recommendation]:
        recs: dict[str, Recommendation] = {}
        for e in self._events():
            if e.get('event') == 'add' and e.get('id') not in recs:
                try:
                    recs[e['id']] = Recommendation(
                        id=e['id'], added=e.get('at', ''), start=date.fromisoformat(e['start']),
                        text=e.get('text', ''), why=e.get('why', ''), judge_by=list(e.get('judge_by', [])),
                        review_days=int(e.get('review_days', DEFAULT_REVIEW_DAYS)),
                        baseline_days=int(e.get('baseline_days', DEFAULT_BASELINE_DAYS)))
                except (KeyError, ValueError):
                    continue
            elif e.get('event') == 'update' and e.get('id') in recs:
                rec = recs[e['id']]
                if e.get('status') in STATUSES:
                    rec.status = e['status']
                rec.notes.append((e.get('at', ''), e.get('status') or '', e.get('note') or ''))
        return list(recs.values())

    def get(self, rec_id: str) -> Recommendation | None:
        return next((r for r in self.all() if r.id == rec_id), None)

    def add(self, text: str, why: str, judge_by: list[dict[str, str]], start: date,
            review_days: int = DEFAULT_REVIEW_DAYS, baseline_days: int = DEFAULT_BASELINE_DAYS,
            now: datetime | None = None) -> Recommendation:
        rec_id = f'rec-{len(self.all()) + 1}'
        self._append({
            'event': 'add', 'id': rec_id, 'at': (now or datetime.now()).isoformat(timespec='seconds'),
            'start': start.isoformat(), 'text': text, 'why': why, 'judge_by': judge_by,
            'review_days': review_days, 'baseline_days': baseline_days,
        })
        return self.get(rec_id)

    def update(self, rec_id: str, status: str | None, note: str | None,
               now: datetime | None = None) -> Recommendation:
        self._append({'event': 'update', 'id': rec_id,
                      'at': (now or datetime.now()).isoformat(timespec='seconds'),
                      'status': status, 'note': note})
        return self.get(rec_id)


@dataclass
class Outcome:
    metric: str
    better: str
    before_mean: float | None
    before_days: int
    after_mean: float | None
    after_days: int
    verdict: str  # better | worse | no clear change | too early | no baseline


def judge(series: dict[date, float], rec: Recommendation, metric: str, better: str,
          last_day: date) -> Outcome:
    """The days since the start against the baseline before it, for one metric."""
    before = [v for d, v in series.items()
              if rec.start - timedelta(days=rec.baseline_days) <= d < rec.start]
    end = min(rec.start + timedelta(days=rec.review_days - 1), last_day)
    after = [v for d, v in series.items() if rec.start <= d <= end]
    b_mean = statistics.mean(before) if before else None
    a_mean = statistics.mean(after) if after else None
    if len(before) < MIN_BASELINE_DAYS:
        verdict = 'no baseline'
    elif len(after) < MIN_AFTER_DAYS:
        verdict = 'too early'
    else:
        sd = statistics.stdev(before)
        delta = a_mean - b_mean
        threshold = max(NOISE_SE * sd * math.sqrt(1 / len(before) + 1 / len(after)), MIN_EFFECT_SD * sd)
        if delta == 0 or abs(delta) < threshold:
            verdict = 'no clear change'
        else:
            verdict = 'better' if _good(delta, better) else 'worse'
    return Outcome(metric, better, b_mean, len(before), a_mean, len(after), verdict)


def _good(delta: float, better: str) -> bool:
    return delta > 0 if better == 'up' else delta < 0
