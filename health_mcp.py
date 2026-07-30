#!/usr/bin/env python3

"""MCP server exposing a local Apple Health export to an LLM.

Speaks JSON-RPC 2.0 over stdio. The protocol surface an MCP server needs is
small and stable, so this implements it directly rather than depending on an
SDK — the whole project installs with `git clone` and nothing else, which
matters more here than usual because the alternative is asking someone to set
up a Python environment before they can look at their own health data.

Reads the CSVs produced by convert_health_data.py. It never touches export.xml
itself: parsing a gigabyte of XML takes ~40 seconds, which no MCP client will
wait for on every call.

    python3 convert_health_data.py --data-dir apple_health_export --out-dir output
    python3 health_mcp.py --data-dir output
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from datetime import date
from typing import Any
from collections.abc import Callable

SERVER_NAME = 'apple-health'
SERVER_VERSION = '2.0.0'

# Protocol revisions this server is known to work against. If a client asks for
# something else it still gets a usable session — the tool surface has not
# changed across these — but it is told which revision it actually got.
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

MAX_ROWS = 400          # hard cap on rows returned by any one call
STALE_AFTER_DAYS = 14   # when to start telling the model the export is old


def log(message: str) -> None:
    """Diagnostics must go to stderr; stdout carries the protocol."""
    print(f'[{SERVER_NAME}] {message}', file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def to_float(value: Any) -> float | None:
    if value in ('', None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_csv(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def read_text(path: str) -> str:
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:
        return f.read()


class HealthData:
    """Lazily-loaded view over the generated outputs."""

    FILES = {
        'daily': 'daily_metrics.csv',
        'insights': 'daily_insights.csv',
        'weekly': 'weekly_summary.csv',
        'workouts': 'workout_summary.csv',
        'coverage': 'metric_coverage.csv',
    }
    DOCS = {
        'context': 'llm_context.md',
        'report': 'insights_report.md',
        'quality': 'data_quality_report.txt',
    }

    def __init__(self, data_dir: str):
        self.dir = os.path.abspath(data_dir)
        self._cache: dict[str, Any] = {}

    # -- loading

    def table(self, name: str) -> list[dict[str, str]]:
        if name not in self._cache:
            self._cache[name] = read_csv(os.path.join(self.dir, self.FILES[name]))
        return self._cache[name]

    def doc(self, name: str) -> str:
        key = f'doc:{name}'
        if key not in self._cache:
            self._cache[key] = read_text(os.path.join(self.dir, self.DOCS[name]))
        return self._cache[key]

    def ready(self) -> bool:
        return bool(self.table('daily'))

    def missing_data_message(self) -> str:
        return (
            f'No health data found in {self.dir}.\n\n'
            'Generate it first:\n'
            '    python3 convert_health_data.py --data-dir /path/to/apple_health_export '
            f'--out-dir {self.dir}\n\n'
            'The export comes from the iPhone Health app: profile icon -> '
            'Export All Health Data, then unzip it.'
        )

    # -- shape

    def metrics(self) -> list[str]:
        rows = self.table('daily')
        if not rows:
            return []
        skip = {'date', 'wear_class', 'sleep_source', 'sleep_onset', 'sleep_wake'}
        return [c for c in rows[0] if c not in skip]

    def dates(self) -> list[date]:
        return [date.fromisoformat(r['date']) for r in self.table('daily') if r.get('date')]

    def series(self, metric: str, start: date | None = None,
               end: date | None = None) -> dict[date, float]:
        out: dict[date, float] = {}
        for row in self.table('daily'):
            if not row.get('date'):
                continue
            d = date.fromisoformat(row['date'])
            if (start and d < start) or (end and d > end):
                continue
            value = to_float(row.get(metric))
            if value is not None:
                out[d] = value
        return out

    def freshness(self) -> tuple[date | None, int | None]:
        dates = self.dates()
        if not dates:
            return None, None
        last = max(dates)
        return last, (date.today() - last).days


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return 'n/a'
    if abs(value - round(value)) < 1e-9 and abs(value) >= 1000:
        return f'{value:,.0f}'
    return f'{value:,.{digits}f}'.rstrip('0').rstrip('.') if digits else f'{value:,.0f}'


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '_no rows_'
    out = ['| ' + ' | '.join(headers) + ' |',
           '|' + '|'.join(['---'] * len(headers)) + '|']
    out += ['| ' + ' | '.join(r) + ' |' for r in rows]
    return '\n'.join(out)


def ordinal(n: int) -> str:
    # 11th/12th/13th are the exceptions that a naive last-digit rule gets wrong.
    suffix = 'th' if 11 <= (n % 100) <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def percentile_of(value: float, population: list[float]) -> float | None:
    if not population:
        return None
    below = sum(1 for v in population if v < value)
    return 100.0 * below / len(population)


def parse_date(raw: str | None, fallback: date | None = None) -> date | None:
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f'"{raw}" is not a date. Use YYYY-MM-DD.') from exc


def describe(values: list[float]) -> str:
    if not values:
        return '_no measured days in range_'
    body = [
        f'- n: **{len(values)}** measured days',
        f'- mean: **{fmt(statistics.mean(values))}**',
        f'- median: **{fmt(statistics.median(values))}**',
        f'- min / max: {fmt(min(values))} / {fmt(max(values))}',
    ]
    if len(values) > 1:
        body.append(f'- stdev: {fmt(statistics.pstdev(values))}')
    return '\n'.join(body)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = []
HANDLERS: dict[str, Callable[[HealthData, dict[str, Any]], str]] = {}


def tool(name: str, description: str, schema: dict[str, Any]):
    def wrap(fn):
        TOOLS.append({'name': name, 'description': description, 'inputSchema': schema})
        HANDLERS[name] = fn
        return fn
    return wrap


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {'type': 'object', 'properties': properties, 'required': required or []}


DATE_PROP = {'type': 'string', 'description': 'YYYY-MM-DD'}
METRIC_PROP = {'type': 'string',
               'description': 'Column name from health_list_metrics, e.g. sleep_deep_hours'}


@tool(
    'health_overview',
    'Start here. Situation summary, analysis window, watch-wear quality, and how the '
    'current 28 days compare with the best the person has ever sustained. Read this '
    'before any other tool so later numbers have a reference point.',
    obj({}),
)
def _overview(data: HealthData, _args: dict[str, Any]) -> str:
    context = data.doc('context')
    if context:
        # The generated pack already opens with situation, inferred events and
        # capacity gap, in that order. Reproducing that logic here would risk
        # the two disagreeing.
        cut = context.find('## Distribution')
        head = context[:cut] if cut > 0 else context
        return head.strip() + '\n\n' + _freshness_note(data)
    return _fallback_overview(data)


def _freshness_note(data: HealthData) -> str:
    last, age = data.freshness()
    if last is None:
        return ''
    if age is not None and age > STALE_AFTER_DAYS:
        return (f'_Data ends {last.isoformat()}, {age} days ago. Anything more recent is not '
                'in this export — re-export from the Health app and re-run the converter._')
    return f'_Data current to {last.isoformat()}._'


def _fallback_overview(data: HealthData) -> str:
    dates = data.dates()
    if not dates:
        return data.missing_data_message()
    wear = {}
    for row in data.table('daily'):
        wear[row.get('wear_class', 'none')] = wear.get(row.get('wear_class', 'none'), 0) + 1
    return (f'{len(dates)} days, {min(dates).isoformat()} to {max(dates).isoformat()}.\n'
            + 'Wear: ' + ', '.join(f'{v} {k}' for k, v in wear.items())
            + '\n\n' + _freshness_note(data))


@tool(
    'health_list_metrics',
    'Every metric available, with how many days carry a value and the date range it '
    'covers. Call this when unsure what a metric is named or whether it exists — '
    'coverage varies enormously by device and by person.',
    obj({'group': {'type': 'string',
                   'description': 'Optional substring filter, e.g. "sleep" or "walking"'}}),
)
def _list_metrics(data: HealthData, args: dict[str, Any]) -> str:
    needle = (args.get('group') or '').lower()
    coverage = {c['column']: c for c in data.table('coverage')}
    rows = []
    for metric in data.metrics():
        if needle and needle not in metric.lower():
            continue
        series = data.series(metric)
        if not series:
            continue
        cov = coverage.get(metric, {})
        rows.append([
            metric,
            str(len(series)),
            min(series).isoformat(),
            max(series).isoformat(),
            cov.get('reliable_start', ''),
        ])
    if not rows:
        return f'No metric matches "{needle}".' if needle else data.missing_data_message()
    rows.sort(key=lambda r: -int(r[1]))
    return (f'{len(rows)} metrics with data.\n\n'
            + table(['metric', 'days', 'first', 'last', 'reliably tracked from'], rows))


@tool(
    'health_metric_stats',
    'Summary statistics for one metric over an optional date range, plus where that '
    'range sits against the person\'s whole history. Use this rather than pulling raw '
    'days when the question is "how much" or "is this normal for me".',
    obj({'metric': METRIC_PROP,
         'start': DATE_PROP,
         'end': DATE_PROP}, ['metric']),
)
def _metric_stats(data: HealthData, args: dict[str, Any]) -> str:
    metric = args['metric']
    start, end = parse_date(args.get('start')), parse_date(args.get('end'))
    scoped = data.series(metric, start, end)
    if not scoped:
        return (f'No measured values for `{metric}` in that range. '
                'Check health_list_metrics for the exact name and its coverage.')

    values = list(scoped.values())
    whole = list(data.series(metric).values())
    mean = statistics.mean(values)
    pct = percentile_of(mean, whole)

    span = f'{min(scoped).isoformat()} to {max(scoped).isoformat()}'
    out = [f'**{metric}** — {span}', '', describe(values)]
    if pct is not None and len(whole) > len(values):
        out.append(f"- this range's mean sits at the **{ordinal(round(pct))} percentile** of all "
                   f'{len(whole)} measured days on record')
    out.append('')
    out.append('_Blank days are excluded, never counted as zero._')
    return '\n'.join(out)


@tool(
    'health_compare_periods',
    'Compare one metric across two date ranges, with the difference and percent change. '
    'The honest way to answer "is this better or worse than before" — pick the comparison '
    'window deliberately rather than trusting a default.',
    obj({'metric': METRIC_PROP,
         'a_start': DATE_PROP, 'a_end': DATE_PROP,
         'b_start': DATE_PROP, 'b_end': DATE_PROP},
        ['metric', 'a_start', 'a_end', 'b_start', 'b_end']),
)
def _compare(data: HealthData, args: dict[str, Any]) -> str:
    metric = args['metric']
    a = data.series(metric, parse_date(args['a_start']), parse_date(args['a_end']))
    b = data.series(metric, parse_date(args['b_start']), parse_date(args['b_end']))
    if not a or not b:
        empty = 'A' if not a else 'B'
        return f'Period {empty} has no measured values for `{metric}`.'

    ma, mb = statistics.mean(a.values()), statistics.mean(b.values())
    delta = mb - ma
    pct = (100.0 * delta / ma) if ma else None
    rows = [
        ['A', f"{args['a_start']}..{args['a_end']}", str(len(a)), fmt(ma)],
        ['B', f"{args['b_start']}..{args['b_end']}", str(len(b)), fmt(mb)],
    ]
    out = [f'**{metric}**', '', table(['period', 'range', 'measured days', 'mean'], rows), '',
           f'B − A = **{delta:+.2f}**' + (f' (**{pct:+.1f}%**)' if pct is not None else '')]
    if min(len(a), len(b)) < 7:
        out.append('')
        out.append(f'_Caution: one period has only {min(len(a), len(b))} measured days. '
                   'That is thin ground for a comparison._')
    return '\n'.join(out)


@tool(
    'health_top_days',
    'The best or worst days for a metric, dated. Anchors a number against what the '
    'person has actually achieved, instead of against a population norm or a recent '
    'average that has drifted.',
    obj({'metric': METRIC_PROP,
         'n': {'type': 'integer', 'description': 'How many days (default 10, max 50)'},
         'order': {'type': 'string', 'enum': ['best', 'worst'],
                   'description': 'best = highest values, worst = lowest. Reverse for '
                                  'metrics where lower is better, like resting_hr.'},
         'start': DATE_PROP, 'end': DATE_PROP},
        ['metric']),
)
def _top_days(data: HealthData, args: dict[str, Any]) -> str:
    metric = args['metric']
    n = max(1, min(int(args.get('n') or 10), 50))
    worst = (args.get('order') or 'best') == 'worst'
    scoped = data.series(metric, parse_date(args.get('start')), parse_date(args.get('end')))
    if not scoped:
        return f'No measured values for `{metric}` in that range.'

    ordered = sorted(scoped.items(), key=lambda kv: kv[1], reverse=not worst)[:n]
    rows = [[d.isoformat(), fmt(v), d.strftime('%a')] for d, v in ordered]
    label = 'Lowest' if worst else 'Highest'
    return (f'**{label} {len(rows)} days for {metric}** (of {len(scoped)} measured)\n\n'
            + table(['date', metric, 'weekday'], rows))


@tool(
    'health_day_detail',
    'Everything recorded for a single day: activity, sleep with stages and timing, '
    'vitals, plus that day\'s deviations from the personal baseline and any strain flag. '
    'Use when a specific date matters — an illness, a race, a bad night.',
    obj({'date': DATE_PROP}, ['date']),
)
def _day_detail(data: HealthData, args: dict[str, Any]) -> str:
    target = parse_date(args['date'])
    row = next((r for r in data.table('daily') if r.get('date') == target.isoformat()), None)
    if not row:
        return f'No row for {target.isoformat()}. The export may not cover that date.'

    filled = [(k, v) for k, v in row.items() if v not in ('', None) and k != 'date']
    lines = [f'# {target.isoformat()} ({target.strftime("%A")})', '']
    lines.append(table(['field', 'value'], [[k, str(v)] for k, v in filled]))

    insight = next((r for r in data.table('insights')
                    if r.get('date') == target.isoformat()), None)
    if insight:
        notable = [(k, v) for k, v in insight.items()
                   if v not in ('', None) and (k.endswith(('_z', '_dev'))
                                               or k.startswith(('recovery', 'strain', 'load')))]
        if notable:
            lines += ['', '## Versus personal baseline', '',
                      table(['field', 'value'], [[k, str(v)] for k, v in notable])]

    workouts = [w for w in data.table('workouts') if w.get('date') == target.isoformat()]
    if workouts:
        lines += ['', '## Workouts', '',
                  table(['activity', 'min', 'kcal', 'avg HR', 'max HR'],
                        [[w.get('activity_type', ''), w.get('duration_min', ''),
                          w.get('total_energy_kcal', ''), w.get('avg_heart_rate_bpm', ''),
                          w.get('max_heart_rate_bpm', '')] for w in workouts])]
    return '\n'.join(lines)


@tool(
    'health_sleep',
    'Sleep across a date range: duration, deep and REM in hours and as a share of total, '
    'efficiency, awakenings, and the clock times of falling asleep and waking. Sleep '
    'timing consistency matters somewhat independently of duration, so both are shown.',
    obj({'start': DATE_PROP, 'end': DATE_PROP,
         'limit': {'type': 'integer', 'description': 'Max nights to list (default 30)'}}),
)
def _sleep(data: HealthData, args: dict[str, Any]) -> str:
    start, end = parse_date(args.get('start')), parse_date(args.get('end'))
    limit = max(1, min(int(args.get('limit') or 30), MAX_ROWS))

    nights = []
    for row in data.table('daily'):
        if not row.get('date') or not row.get('sleep_asleep_hours'):
            continue
        d = date.fromisoformat(row['date'])
        if (start and d < start) or (end and d > end):
            continue
        nights.append(row)
    if not nights:
        return 'No staged sleep in that range.'

    shown = nights[-limit:]
    rows = [[r['date'], r.get('sleep_asleep_hours', ''), r.get('sleep_deep_hours', ''),
             r.get('sleep_rem_hours', ''), r.get('sleep_deep_pct', ''),
             r.get('sleep_efficiency_pct', ''), r.get('sleep_onset', ''),
             r.get('sleep_wake', ''), r.get('sleep_awakenings', '')] for r in shown]

    def col(key):
        return [to_float(r.get(key)) for r in nights if to_float(r.get(key)) is not None]

    summary = []
    for key, label in (('sleep_asleep_hours', 'total'), ('sleep_deep_hours', 'deep'),
                       ('sleep_rem_hours', 'REM'), ('sleep_efficiency_pct', 'efficiency')):
        vals = col(key)
        if vals:
            summary.append(f'{label} {fmt(statistics.mean(vals))}')

    return (f'**{len(nights)} nights**, showing last {len(shown)}. '
            f'Means: {", ".join(summary)}.\n\n'
            + table(['date', 'asleep h', 'deep h', 'REM h', 'deep %', 'eff %',
                     'asleep at', 'woke', 'wakes'], rows))


@tool(
    'health_workouts',
    'Recorded workouts, optionally filtered by date or activity. Minutes alone cannot '
    'tell rehabilitation from training — check the activity mix before treating volume '
    'as fitness work.',
    obj({'start': DATE_PROP, 'end': DATE_PROP,
         'activity': {'type': 'string', 'description': 'Substring, e.g. "Strength", "Cycling"'},
         'summary_only': {'type': 'boolean',
                          'description': 'Totals by activity type instead of individual sessions'}}),
)
def _workouts(data: HealthData, args: dict[str, Any]) -> str:
    start, end = parse_date(args.get('start')), parse_date(args.get('end'))
    needle = (args.get('activity') or '').lower()

    picked = []
    for w in data.table('workouts'):
        if not w.get('date'):
            continue
        d = date.fromisoformat(w['date'])
        if (start and d < start) or (end and d > end):
            continue
        if needle and needle not in w.get('activity_type', '').lower():
            continue
        picked.append(w)
    if not picked:
        return 'No workouts match.'

    if args.get('summary_only'):
        agg: dict[str, list[float]] = {}
        for w in picked:
            agg.setdefault(w.get('activity_type', '?'), []).append(
                to_float(w.get('duration_min')) or 0.0)
        rows = [[k, str(len(v)), fmt(sum(v), 0), fmt(statistics.mean(v), 0)]
                for k, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]))]
        return (f'**{len(picked)} workouts**\n\n'
                + table(['activity', 'sessions', 'total min', 'mean min'], rows))

    shown = picked[-MAX_ROWS:]
    rows = [[w['date'], w.get('activity_type', ''), w.get('duration_min', ''),
             w.get('total_energy_kcal', ''), w.get('avg_heart_rate_bpm', ''),
             w.get('cycling_power_avg_w', '')] for w in shown]
    return (f'**{len(picked)} workouts**, showing last {len(shown)}\n\n'
            + table(['date', 'activity', 'min', 'kcal', 'avg HR', 'avg W'], rows))


@tool(
    'health_strain_episodes',
    'Periods where several recovery signals moved the wrong way together — elevated '
    'resting heart rate and wrist temperature, depressed HRV, raised respiratory rate. '
    'This is the pattern wearables use for illness onset. It is a prompt to ask what '
    'happened, never a diagnosis.',
    obj({}),
)
def _episodes(data: HealthData, _args: dict[str, Any]) -> str:
    context = data.doc('context')
    if context and '## Physiological strain episodes' in context:
        start = context.index('## Physiological strain episodes')
        rest = context[start:]
        nxt = rest.find('\n## ', 3)
        return rest[:nxt].strip() if nxt > 0 else rest.strip()

    flagged = [r for r in data.table('insights') if r.get('strain_flag') == 'yes']
    if not flagged:
        return 'No days where two or more strain signals moved together.'
    rows = [[r['date'], r.get('strain_signal_count', ''), r.get('strain_detail', '')]
            for r in flagged[-40:]]
    return (f'**{len(flagged)} flagged days**\n\n'
            + table(['date', 'signals', 'detail'], rows))


@tool(
    'health_weekly',
    'Week-by-week rollup — training volume, session counts, sleep, resting HR, HRV, '
    'weight. Better than daily rows for spotting multi-week direction without drowning '
    'in noise.',
    obj({'weeks': {'type': 'integer', 'description': 'Most recent N weeks (default 12)'}}),
)
def _weekly(data: HealthData, args: dict[str, Any]) -> str:
    rows_all = data.table('weekly')
    if not rows_all:
        return data.missing_data_message()
    n = max(1, min(int(args.get('weeks') or 12), 200))
    shown = rows_all[-n:]
    keys = ['iso_week', 'total_exercise_minutes', 'workouts_total', 'total_steps',
            'avg_sleep_hours', 'avg_deep_sleep_hours', 'avg_resting_hr', 'avg_hrv_sdnn',
            'weight_week_avg']
    present = [k for k in keys if any(r.get(k) for r in shown)]
    return (f'**Last {len(shown)} weeks**\n\n'
            + table(present, [[r.get(k, '') for k in present] for r in shown]))


@tool(
    'health_context_pack',
    'The complete pre-computed briefing: capacity gap, inferred events, personal '
    'records, load eras, distributions, streaks, correlations and the explicit limits '
    'of what this data can support. Large — prefer the targeted tools unless a full '
    'picture is genuinely needed.',
    obj({}),
)
def _context_pack(data: HealthData, _args: dict[str, Any]) -> str:
    doc = data.doc('context')
    return doc if doc else data.missing_data_message()


@tool(
    'health_data_quality',
    'How trustworthy the underlying data is: watch-wear rates, per-metric coverage, '
    'excluded sensor artifacts, and which device recorded what. Check this before '
    'leaning hard on any single metric.',
    obj({}),
)
def _quality(data: HealthData, _args: dict[str, Any]) -> str:
    doc = data.doc('quality')
    if not doc:
        return data.missing_data_message()
    return doc[:8000] + ('\n\n_(truncated)_' if len(doc) > 8000 else '')


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def make_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def make_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def handle(message: dict[str, Any], data: HealthData) -> dict[str, Any] | None:
    method = message.get('method')
    request_id = message.get('id')
    params = message.get('params') or {}

    # Notifications carry no id and must never be answered.
    if request_id is None and method != 'initialize':
        return None

    if method == 'initialize':
        asked = (params.get('protocolVersion') or '').strip()
        version = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        log(f'initialize (client asked {asked or "nothing"}, serving {version})')
        return make_result(request_id, {
            'protocolVersion': version,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': (
                'Local Apple Health data for one person. Call health_overview first: it '
                'establishes the analysis window and how the present compares with what '
                'this person has previously sustained, without which individual numbers '
                'have no reference point. Blank values mean "not measured", never zero. '
                'Nothing here is causal and nothing is a diagnosis — where the data shows '
                'a change, ask what happened rather than inferring a cause.'
            ),
        })

    if method == 'ping':
        return make_result(request_id, {})

    if method == 'tools/list':
        return make_result(request_id, {'tools': TOOLS})

    if method == 'tools/call':
        name = params.get('name', '')
        args = params.get('arguments') or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return make_error(request_id, -32602, f'Unknown tool: {name}')
        if not data.ready():
            return make_result(request_id, {
                'content': [{'type': 'text', 'text': data.missing_data_message()}],
                'isError': True,
            })
        try:
            text = handler(data, args)
        except ValueError as exc:
            # Bad input from the model: report it as tool output so it can retry
            # with corrected arguments rather than seeing a protocol failure.
            return make_result(request_id, {
                'content': [{'type': 'text', 'text': f'Invalid argument: {exc}'}],
                'isError': True,
            })
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the session
            log(f'tool {name} failed: {exc!r}')
            return make_result(request_id, {
                'content': [{'type': 'text', 'text': f'{name} failed: {exc}'}],
                'isError': True,
            })
        return make_result(request_id, {'content': [{'type': 'text', 'text': text}]})

    return make_error(request_id, -32601, f'Method not found: {method}')


def serve(data: HealthData) -> None:
    log(f'serving {data.dir}')
    if not data.ready():
        log('WARNING: no data found yet — tools will explain how to generate it')

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f'bad JSON: {exc}')
            continue

        try:
            response = handle(message, data)
        except Exception as exc:  # noqa: BLE001 - keep the session alive
            log(f'handler error: {exc!r}')
            response = make_error(message.get('id'), -32603, str(exc))

        if response is not None:
            sys.stdout.write(json.dumps(response) + '\n')
            sys.stdout.flush()

    log('stdin closed, exiting')


def client_config(data_dir: str) -> str:
    return json.dumps({
        'mcpServers': {
            SERVER_NAME: {
                'command': sys.executable,
                'args': [os.path.abspath(__file__), '--data-dir', os.path.abspath(data_dir)],
            }
        }
    }, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='MCP server exposing a local Apple Health export.',
        epilog='Generate the data first with convert_health_data.py.',
    )
    parser.add_argument('--data-dir', default='output',
                        help='Directory holding the generated CSVs (default: output)')
    parser.add_argument('--print-config', action='store_true',
                        help='Print the JSON block to add to your MCP client config, then exit')
    parser.add_argument('--check', action='store_true',
                        help='Verify the data loads and list the tools, then exit')
    args = parser.parse_args()

    data = HealthData(args.data_dir)

    if args.print_config:
        print(client_config(args.data_dir))
        return

    if args.check:
        if not data.ready():
            print(data.missing_data_message())
            raise SystemExit(1)
        last, age = data.freshness()
        print(f'OK  {len(data.dates())} days in {data.dir}, ending {last} ({age} days ago)')
        print(f'    {len(data.metrics())} metrics, {len(data.table("workouts"))} workouts')
        print(f'    {len(TOOLS)} tools: ' + ', '.join(t['name'] for t in TOOLS))
        return

    serve(data)


if __name__ == '__main__':
    main()
