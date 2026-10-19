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
from datetime import date, datetime
from typing import Any
from collections.abc import Callable

SERVER_NAME = 'apple-health'
SERVER_VERSION = '2.1.0'

# Protocol revisions this server is known to work against. If a client asks for
# something else it still gets a usable session — the tool surface has not
# changed across these — but it is told which revision it actually got.
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

MAX_ROWS = 400          # hard cap on rows returned by any one call
STALE_AFTER_DAYS = 14   # when to start telling the model the export is old

# Ceiling on a single tool result, in characters. Generous by default; a local
# model with an 8K window needs roughly 6000 here or one call consumes the whole
# context before it can answer. Overridden by --max-output-chars.
DEFAULT_MAX_OUTPUT_CHARS = 20000
MAX_OUTPUT_CHARS = DEFAULT_MAX_OUTPUT_CHARS


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


def _size_and_mtime(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


class HealthData:
    """Lazily-loaded view over the generated outputs."""

    FILES = {
        'daily': 'daily_metrics.csv',
        'insights': 'daily_insights.csv',
        'weekly': 'weekly_summary.csv',
        'workouts': 'workout_summary.csv',
        'series': 'workout_series.csv',
        'coverage': 'metric_coverage.csv',
    }
    DOCS = {
        'context': 'llm_context.md',
        'report': 'insights_report.md',
        'quality': 'data_quality_report.txt',
        'status': 'data_status.json',
    }

    def __init__(self, data_dir: str):
        self.root = os.path.abspath(data_dir)
        self.dir = os.path.realpath(self.root)
        self._cache: dict[str, Any] = {}
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> tuple:
        return tuple(_size_and_mtime(os.path.join(self.dir, name))
                     for name in (*self.FILES.values(), *self.DOCS.values()))

    def follow(self) -> None:
        """Drop cached tables if the data on disk has changed since they were read.

        Called once at the start of each tool call. Tables are loaded lazily
        and cached for the session, which without this meant a rebuild was
        invisible until the client was restarted. Two ways the data changes:

        * health_sync.py keeps data_dir as a link it switches to each new
          rebuild; the link now points somewhere else.
        * A scheduled convert_health_data.py rewrote the files in place.

        Checking here rather than on every read keeps one answer to one
        rebuild: following a moved link mid-call could pair this hour's daily
        metrics with last hour's weekly summary.
        """
        target = os.path.realpath(self.root)
        if target != self.dir:
            log(f'data moved to {target}')
            self.dir = target
            self._cache.clear()
        stamp = self._file_stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            self._cache.clear()

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
        parent = os.path.dirname(self.root)
        if os.path.isdir(os.path.join(parent, 'snapshots')) or os.path.exists(
                os.path.join(parent, '.lock')):
            # A health_sync.py output folder: telling the model to run the
            # converter by hand would be wrong advice; the loop is doing it.
            return (
                f'No health data in {self.root} yet. health_sync.py is set up to write '
                'it but has not finished a rebuild: either the first one is still '
                'running, or it is failing. Its log says which.'
            )
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


def truncate(text: str, hint: str = '') -> str:
    """Cut an over-long result at a line boundary and say so.

    Silently dropping the tail is worse than useless: the model cannot tell a
    truncated table from a complete one, and will happily conclude that the
    last row it can see is the last row that exists.
    """
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    clipped = text[:MAX_OUTPUT_CHARS]
    cut = clipped.rfind('\n')
    if cut > MAX_OUTPUT_CHARS // 2:
        clipped = clipped[:cut]
    dropped = len(text) - len(clipped)
    note = (f'\n\n_[truncated: {dropped:,} more characters. This output was cut to fit a '
            f'{MAX_OUTPUT_CHARS:,}-character limit, so it is INCOMPLETE — do not treat the '
            'last row as the last that exists.')
    note += f' {hint}]_' if hint else ']_'
    return clipped + note


def markdown_sections(doc: str) -> dict[str, str]:
    """Split a generated report into its top-level `## ` sections."""
    sections: dict[str, str] = {}
    current, body = None, []
    for line in doc.splitlines():
        if line.startswith('## '):
            if current:
                sections[current] = '\n'.join(body).strip()
            current, body = line[3:].strip(), [line]
        elif current:
            body.append(line)
    if current:
        sections[current] = '\n'.join(body).strip()
    return sections


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
# Freshness: said on every answer
# ---------------------------------------------------------------------------

def data_status(data: HealthData) -> dict[str, Any]:
    try:
        return json.loads(data.doc('status') or '{}')
    except ValueError:
        return {}


def _local_now(status: dict[str, Any], now: datetime | None = None) -> datetime:
    zone = None
    if status.get('timezone'):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(status['timezone'])
        except Exception:  # noqa: BLE001 - unknown zone: fall back to the machine's
            zone = None
    now = now or datetime.now().astimezone()
    return now.astimezone(zone) if zone else now.astimezone()


def _when(raw: str, now: datetime) -> str:
    """'22:41' if today, else '2026-10-18 22:41', in the data's time zone."""
    try:
        moment = datetime.fromisoformat(raw).astimezone(now.tzinfo)
    except (TypeError, ValueError):
        return ''
    return moment.strftime('%H:%M') if moment.date() == now.date() else moment.strftime('%Y-%m-%d %H:%M')


def status_line(data: HealthData, now: datetime | None = None) -> str:
    """One line on how fresh the data is, put above every tool answer.

    Without it a missing ride and a ride that hasn't synced yet look the same,
    and so do a finished day and one still under way.
    """
    status = data_status(data)
    if not status:
        return ''
    now = _local_now(status, now)
    parts = []
    if status.get('source') == 'health_bridge':
        if status.get('latest_received'):
            parts.append(f"phone last synced {_when(status['latest_received'], now)}")
        if status.get('latest_record'):
            parts.append(f"newest record {_when(status['latest_record'], now)}")
        if status.get('built_at'):
            parts.append(f"rebuilt {_when(status['built_at'], now)}")
        if status.get('check_interval_s'):
            parts.append(f"the server checks for new data every {round(status['check_interval_s'] / 60)} min")
    else:
        parts.append('from an Apple Health export, not live')
        if status.get('latest_record'):
            parts.append(f"newest record {_when(status['latest_record'], now)}")
    line = 'Data: ' + '; '.join(parts) + '.'
    today = now.date().isoformat()
    if any(r.get('date') == today for r in data.table('daily')):
        line += (f" Today ({now:%a %d %b}) is still in progress: its totals are "
                 'partial, not final.')
    return f'_{line}_'


def is_today(data: HealthData, day: date, now: datetime | None = None) -> bool:
    return _local_now(data_status(data), now).date() == day


# ---------------------------------------------------------------------------
# Workouts: one layout everywhere
# ---------------------------------------------------------------------------

WORKOUT_HEADERS = ['date', 'time', 'activity', 'min', 'avg HR', 'max HR',
                   'HR zone min z1/z2/z3/z4/z5', 'avg W', 'max W', 'rpm',
                   'HR / power from', 'flag']


def _clock(raw: str) -> str:
    """'2026-10-19 22:23:00 +0300' -> '22:23' (local time as recorded)."""
    parts = (raw or '').split(' ')
    return parts[1][:5] if len(parts) > 1 else ''


def _zones(w: dict[str, str]) -> str:
    values = [to_float(w.get(f'hr_zone_z{i}_min')) for i in range(1, 6)]
    if not any(v for v in values if v):
        return ''
    return '/'.join(str(round(v)) if v else '0' for v in values)


def _sources(w: dict[str, str]) -> str:
    hr, power = w.get('hr_source', ''), w.get('power_source', '')
    if hr and power and hr != power:
        return f'{hr} / {power}'
    return hr or power or w.get('source_name', '')


def workout_row(w: dict[str, str]) -> list[str]:
    return [w.get('date', ''),
            f"{_clock(w.get('start_datetime', ''))}–{_clock(w.get('end_datetime', ''))}",
            w.get('activity_type', ''), fmt(to_float(w.get('duration_min')), 0),
            w.get('avg_heart_rate_bpm', ''), w.get('max_heart_rate_bpm', ''), _zones(w),
            w.get('cycling_power_avg_w', ''), w.get('cycling_power_max_w', ''),
            w.get('cycling_cadence_avg_rpm', ''), _sources(w),
            ('suspicious: ' + w['quality_flag']) if w.get('quality_flag') else '']


def workout_table(workouts: list[dict[str, str]]) -> str:
    return table(WORKOUT_HEADERS, [workout_row(w) for w in workouts])


def series_findings(minutes: list[dict[str, float | None]]) -> list[str]:
    """What the minute-by-minute numbers say that the averages don't.

    `minutes`: dicts with minute, hr, power (either may be None), in order.
    """
    def mean(values):
        return statistics.mean(values) if values else None

    findings = []
    paired = [(m['hr'], m['power']) for m in minutes if m.get('hr') and m.get('power')]
    if len(paired) >= 20:
        half = len(paired) // 2
        first, second = paired[:half], paired[half:]
        p1, p2 = mean([p for _h, p in first]), mean([p for _h, p in second])
        h1, h2 = mean([h for h, _p in first]), mean([h for h, _p in second])
        decoupling = ((p1 / h1) - (p2 / h2)) / (p1 / h1) * 100
        powers = [p for _h, p in paired]
        cv = statistics.pstdev(powers) / statistics.mean(powers)
        text = (f'Heart-rate drift at steady power (aerobic decoupling): {decoupling:+.1f}%. '
                f'First half {p1:.0f} W at {h1:.0f} bpm, second half {p2:.0f} W at {h2:.0f} bpm. '
                'Under about 5% means heart rate held at the same power; above it, it '
                'climbed (fatigue, heat or too little fuel or water).')
        if cv > 0.2:
            text += f' Power varied a lot (CV {cv * 100:.0f}%), so treat this loosely.'
        findings.append(text)
    else:
        hrs = [m['hr'] for m in minutes if m.get('hr')]
        if len(hrs) >= 10:
            half = len(hrs) // 2
            why = (f'only {len(paired)} minutes have both heart rate and power, too few to '
                   'judge drift at steady power' if paired else 'no power to compare against')
            findings.append(f'Heart rate averaged {mean(hrs[:half]):.0f} bpm in the first half and '
                            f'{mean(hrs[half:]):.0f} bpm in the second ({why}).')

    by_minute = {m['minute']: m for m in minutes}
    with_power = [m for m in minutes if m.get('power')]
    peak = (max(with_power, key=lambda m: m['power']) if with_power
            else max((m for m in minutes if m.get('hr')), key=lambda m: m['hr'], default=None))
    if peak and peak.get('hr'):
        after = [by_minute.get(peak['minute'] + k) for k in (1, 2)]
        label = f"{peak['power']:.0f} W, " if peak.get('power') else ''
        text = f"Hardest minute: minute {peak['minute']} ({label}{peak['hr']:.0f} bpm)."
        if after[0] and after[0].get('hr'):
            text += f" One minute later {after[0]['hr']:.0f} bpm ({after[0]['hr'] - peak['hr']:+.0f})"
            if after[1] and after[1].get('hr'):
                text += f", two minutes later {after[1]['hr']:.0f} bpm ({after[1]['hr'] - peak['hr']:+.0f})"
            text += '. Minute averages, so the true recovery is a little faster than this shows.'
        else:
            text += ' No heart rate recorded in the minute after it, so recovery can\'t be read.'
        findings.append(text)
    return findings


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
        return truncate(head.strip() + '\n\n' + _freshness_note(data),
                        'Use health_context_pack for specific sections.')
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
    heading = f'# {target.isoformat()} ({target.strftime("%A")})'
    if is_today(data, target):
        heading += ' — in progress, totals so far'
    lines = [heading, '']
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
        lines += ['', '## Workouts', '', workout_table(workouts),
                  '', 'Minute by minute: health_workout_detail with this date.']
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
    flagged = sum(1 for w in picked if w.get('quality_flag'))
    note = (f' {flagged} look mis-recorded (see the flag column); leave them out of '
            'comparisons.' if flagged else '')
    return (f'**{len(picked)} workouts**, showing last {len(shown)}.{note}\n\n'
            + workout_table(shown))


@tool(
    'health_workout_detail',
    'One workout minute by minute: heart rate, power and cadence, plus heart-rate drift '
    'at steady power (aerobic decoupling, a sign of fatigue) and how fast heart rate '
    'came down after the hardest minute. Give the date; if that day had more than one '
    'workout, also its start time.',
    obj({'date': DATE_PROP,
         'start': {'type': 'string', 'description': 'Start time HH:MM, when the day had several workouts'}},
        ['date']),
)
def _workout_detail(data: HealthData, args: dict[str, Any]) -> str:
    target = parse_date(args['date'])
    day = [w for w in data.table('workouts') if w.get('date') == target.isoformat()]
    if not day:
        return f'No workouts on {target.isoformat()}.'
    start = (args.get('start') or '').strip()
    picked = [w for w in day if start and _clock(w.get('start_datetime', '')) == start]
    if not picked and len(day) == 1 and not start:
        picked = day
    if not picked:
        return (f'{len(day)} workouts on {target.isoformat()}; say which by its start time.\n\n'
                + workout_table(day))
    w = picked[0]

    lines = [workout_table([w])]
    if w.get('quality_flag'):
        lines += ['', f"This workout looks mis-recorded ({w['quality_flag']}); its numbers may not be real."]
    rows = [r for r in data.table('series') if r.get('workout_id') == w.get('workout_id')]
    if not rows:
        return '\n'.join(lines + ['', 'No heart-rate or power samples were recorded inside this workout.'])

    minutes = [{'minute': int(r['minute']), 'hr': to_float(r.get('hr_avg_bpm')),
                'hr_max': to_float(r.get('hr_max_bpm')), 'power': to_float(r.get('power_avg_w')),
                'power_max': to_float(r.get('power_max_w')),
                'cadence': to_float(r.get('cadence_avg_rpm'))} for r in rows]
    findings = series_findings(minutes)
    if findings:
        lines += [''] + [f'- {f}' for f in findings]

    # One row per minute up to an hour and a half; five-minute blocks beyond, so a
    # long ride still fits in one answer.
    step = 1 if minutes[-1]['minute'] <= 90 else 5
    blocks: dict[int, list[dict[str, Any]]] = {}
    for m in minutes:
        blocks.setdefault(m['minute'] // step * step, []).append(m)

    def avg(ms, key):
        vals = [m[key] for m in ms if m.get(key) is not None]
        return fmt(statistics.mean(vals), 0) if vals else ''

    def peak(ms, key):
        vals = [m[key] for m in ms if m.get(key) is not None]
        return fmt(max(vals), 0) if vals else ''

    table_rows = [[str(b), avg(ms, 'hr'), peak(ms, 'hr_max'), avg(ms, 'power'), peak(ms, 'power_max'),
                   avg(ms, 'cadence')] for b, ms in sorted(blocks.items())]
    label = 'minute' if step == 1 else 'from minute (5-min blocks)'
    lines += ['', table([label, 'avg HR', 'max HR', 'avg W', 'max W', 'rpm'], table_rows)]
    return '\n'.join(lines)


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
    'The pre-computed briefing: capacity gap, inferred events, personal records, load '
    'eras, distributions, streaks, correlations, and the explicit limits of this data. '
    'Call with no arguments to list the sections, then request one by name — the whole '
    'pack is large and will crowd out a small context window.',
    obj({'section': {'type': 'string',
                     'description': 'Section name, or a substring of one. Omit to list '
                                    'the available sections with their sizes.'}}),
)
def _context_pack(data: HealthData, args: dict[str, Any]) -> str:
    doc = data.doc('context')
    if not doc:
        return data.missing_data_message()

    sections = markdown_sections(doc)
    wanted = (args.get('section') or '').strip().lower()

    if not wanted:
        rows = [[name, f'{len(body):,}'] for name, body in sections.items()]
        preamble = doc[:doc.find('## ')].strip() if '## ' in doc else ''
        return (preamble + '\n\n' if preamble else '') + \
            f'**{len(sections)} sections available.** Request one by name.\n\n' + \
            table(['section', 'characters'], rows) + \
            '\n\n_Pass `section` to fetch one. Ask for "all" to get the whole pack._'

    if wanted == 'all':
        return truncate(doc, 'Request individual sections instead.')

    for name, body in sections.items():
        if wanted == name.lower() or wanted in name.lower():
            return truncate(body)
    return (f'No section matching "{args.get("section")}". Available: '
            + ', '.join(sections) + '.')


@tool(
    'health_profile',
    "The subject's own stated goals, constraints and context — age, injuries, what "
    'they are training for, what they have deliberately stopped. The export records '
    'what a body did and can never record why; this is the only source for that. Read '
    'it before interpreting any decline or change.',
    obj({}),
)
def _profile(data: HealthData, _args: dict[str, Any]) -> str:
    doc = data.doc('context')
    sections = markdown_sections(doc) if doc else {}
    for name, body in sections.items():
        if 'own words' in name.lower() or name.lower().startswith('no profile'):
            return body
    return ('No profile has been supplied. Do not infer goals, discipline or motivation '
            'from the numbers — ask. A profile.md can be added next to the data '
            '(see profile.example.md in the project).')


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
        data.follow()
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
        # Every answer says how fresh the data is, so "missing" and "not synced
        # yet" are never confused.
        status = status_line(data)
        if status:
            text = f'{status}\n\n{text}'
        # Capped here rather than in each tool: a new tool would otherwise be one
        # oversight away from flooding a small context window.
        return make_result(request_id, {'content': [{'type': 'text',
                                                     'text': truncate(text)}]})

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
    parser.add_argument('--max-output-chars', type=int, default=DEFAULT_MAX_OUTPUT_CHARS,
                        help='Ceiling on a single tool result (default '
                             f'{DEFAULT_MAX_OUTPUT_CHARS}). Set ~6000 for a local model '
                             'with an 8K context window.')
    parser.add_argument('--print-config', action='store_true',
                        help='Print the JSON block to add to your MCP client config, then exit')
    parser.add_argument('--check', action='store_true',
                        help='Verify the data loads and list the tools, then exit')
    args = parser.parse_args()

    global MAX_OUTPUT_CHARS
    MAX_OUTPUT_CHARS = max(1000, args.max_output_chars)

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
        print(f'    {len(TOOLS)} tools, output capped at {MAX_OUTPUT_CHARS:,} chars')
        print('    ' + ', '.join(t['name'] for t in TOOLS))
        return

    serve(data)


if __name__ == '__main__':
    main()
