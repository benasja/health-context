# How it works

What the converter does to an export before anything reads it, and why each
step exists. Every one of them fixes a number that used to come out plausible
and wrong.

## The parts that matter

### It finds your own start date

A new watch backfills nothing, so metrics begin when the hardware does. But an
app installed once and abandoned leaves a scatter of samples across earlier
years. For each metric the tool finds the **reliable start**: the first date
where at least half of the following 28 days carry a value. The analysis window
then opens where the continuously-tracked core metrics (resting HR, HRV, staged
sleep) are *all* live. Nothing is hardcoded.

### It knows when you weren't wearing the watch

`wear_hours` counts distinct clock hours with a sample that requires skin
contact — heart rate, physical effort, stand time. Days are classed `full`
(>=18h), `partial` (>=8h), `minimal` or `none`, so non-wear days stop silently
dragging down averages.

Deliberately excluded from this signal: basal and active energy. Basal energy is
a model (BMR x elapsed time) the watch emits around the clock whether or not it
is on your wrist, so counting it marks every day as fully worn and defeats the
entire point.

### It de-duplicates across devices

An iPhone in a pocket and a watch on a wrist count the same steps. Summing the
export's raw records inflates step totals by roughly a third. The tool takes
sources best-first (watch, then phone, then anything else) and counts each
stretch of time only once — so the phone still fills in hours the watch was off,
without double-counting the hours it wasn't.

Applied to steps, walking distance, flights climbed and active energy. Dietary
metrics get the same treatment via a per-day primary source, since two food
apps mirroring the same meals would otherwise double every total.

### It picks one sleep source per night

Nights are often logged by the watch, the phone and a third-party tracker at
once. Rather than summing overlapping segments, one authoritative source wins
per night: staged data (REM/Core/Deep) beats unstaged, and among equals the one
with the most recorded sleep wins.

### It refuses to report trends it cannot support

Weight, body fat and VO2 max are measured occasionally. Carrying the last
reading forward is right for estimating *today's* value, but averaging those
carried rows turns two measurements into a confident-looking multi-week trend.
Trends for these are withheld unless there are at least 3 genuine measurements
in each period, and the report says which were withheld and why.

The quality report also flags metrics recorded by more than one device, because
two bioimpedance scales disagreeing by several percentage points is not fat loss.

### Max heart rate is inferred, not assumed

Zones are only as good as the ceiling they are measured against. The tool uses
the highest heart rate anywhere in the export — including `WorkoutStatistics`
maxima, which often hold peaks the periodic sample stream misses — but discards
an isolated peak standing more than 10 bpm above every other observation, since
wrist optical sensors throw artifacts during strength work. Override with
`--max-hr` if you know your true max.

## What gets computed

**Recovery score (0-100)** — weighted blend of HRV (30%), resting HR (25%),
sleep duration (25%), sleep efficiency (10%) and respiratory rate (10%), each
scored against your own 60-day rolling baseline, where 70 means "at baseline".
The component list is emitted alongside so the number is auditable.

**Strain flags** — days where two or more of {wrist temperature up, resting HR
up, HRV down, respiratory rate up, SpO2 down} moved together against your
baseline. Any one of these moves for boring reasons; the combination is the
pattern consumer wearables use for illness onset.

**Sleep regularity** — standard deviation of sleep midpoint over 14 days,
computed with circular statistics so times either side of midnight compare
correctly.

**Training load** — acute (7-day) to chronic (28-day) exercise-minute ratio.
The 0.8–1.5 band is the commonly cited sweet spot. Below a 28-day mean of
21 min/day (~150 min/week, the WHO activity guideline) the ratio is reported
but deliberately **not** classified: at 10 min/day a single 45-minute session
moves it across a whole category, so the label would describe one workout
rather than a training pattern. The ratio is a contested heuristic from
team-sport research and assumes near-daily training.

**Cycling power** — where a trainer or power meter recorded watts, the
duration-weighted average is reported against your FTP. Power is *measured*
rather than inferred, so unlike heart rate it does not drift with heat,
caffeine, sleep or stress — it is the most reliable intensity figure an Apple
Health export contains. Roughly: under 55% of FTP is recovery riding, 56–75%
endurance, 76–90% tempo, above 90% threshold.

**Associations** — lagged correlations between what you do and what your body
does next (sleep → next-day HRV, steps → next-night deep sleep, and so on).
Reported with `n` and a significance marker; relationships with fewer than 30
paired days are omitted rather than reported weakly.

**Capacity gap** — your current 28-day mean against the best 28-day mean you
have ever held, with the date you held it. A rolling baseline drifts with you,
so on its own it will describe a two-year decline as "slightly worse than
lately"; this is the number that says 10 min/day is 15% of what you have
already proven you can sustain.

**Personal records, eras, streaks** — best day, best rolling 7 and 28 days,
best ISO week and best calendar month for each key metric, all dated; the
timeline cut into contiguous load regimes with the sleep, HRV, resting HR and
weight that accompanied each; and longest/current streaks against plain
thresholds. Percentile tables make every other number in the pack readable, and
scattered strain days are grouped into episodes with start, end and duration.

## Metrics captured

Around 45 HealthKit types across activity, cardiovascular, respiratory, sleep,
body composition, mobility/gait, nutrition, environment and mindfulness —
including physical effort (METs), SpO2, sleeping wrist temperature, breathing
disturbances, heart-rate recovery, time in daylight, walking speed/asymmetry/
double-support/steadiness, stair speeds and AFib burden.

Run once and read `metric_coverage.csv` for exactly what your own export
contains. Adding another metric is a single `MetricSpec` entry in
`health_metrics.py`.

> **Percent units:** HealthKit stores `%` as a fraction (`0.183` = 18.3%). Every
> percent-valued metric is scaled to real percentages on the way out. Body fat
> read straight from the XML looks like 0.18%.

## `--include-cda` warning

`export_cda.xml` is an HL7 clinical-document mirror of `export.xml`. Its records
carry different date formats and source names, so they cannot be matched against
the main export for de-duplication — including it **double-counts** every summed
metric. Off by default; only enable it if `export.xml` is missing.

## Code layout

| Module | Role |
|--------|------|
| `health_metrics.py` | Declarative metric registry — units, aggregation, grouping |
| `health_coaching.py` | Parsing, daily/weekly/workout aggregation, de-duplication, coverage |
| `health_insights.py` | Baselines, z-scores, scores, flags, trends, associations, reports |
| `health_history.py` | Long memory — records, capacity gap, eras, streaks, strain episodes, distributions |
| `convert_health_data.py` | CLI entry point, CDA and ECG handling |
| `health_bridge_import.py` | Reads a Health Bridge receiver's database in place of an export |
| `health_hr_timeline.py` | Heart rate and steps through the day in 5-minute buckets, with sleep, workout and step context |
| `health_recs.py` | The suggestions log: kept as events, judged against the weeks before |
| `health_strength.py` | Sets logged in Wariden, from the receiver, into the strength CSVs |
| `health_sync.py` | Unattended rebuild loop for a receiver, with atomic snapshots |
| `health_mcp.py` | MCP server over the generated files |
| `health_watch.py` | With `--export`: finds the newest export in a folder and keeps the server's data built from it |
| `extension/build.py` | Packs the Claude Desktop extension (`.mcpb`) |
