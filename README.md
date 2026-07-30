# Apple Health XML to CSV Converter

Turns an Apple Health export (`export.xml`) into analysis-ready CSVs **and** a
computed insights report — baselines, deviations, trends and personal
associations, rather than a pile of rows to eyeball.

## Quick start

1. Health app → profile picture → **Export All Health Data**.
2. Unzip and put `export.xml` next to `convert_health_data.py` (optionally
   `export_cda.xml` and `electrocardiograms/` too).
3. Run:

```bash
python3 convert_health_data.py
```

A ~1 GB export takes about a minute and shows a progress bar while parsing.

### Options

| Flag | Purpose |
|------|---------|
| `--data-dir DIR` | Where `export.xml` lives (default: next to the script) |
| `--out-dir DIR` | Where to write outputs (default: same as `--data-dir`) |
| `--max-hr BPM` | Max heart rate for zone maths. Default: highest *corroborated* value in the export |
| `--age N` | Sanity-checks max HR against `208 - 0.7 x age` |
| `--skip-legacy` | Skip the large flat `full_health_data.csv` (much faster) |
| `--include-cda` | Also parse `export_cda.xml` — **see the warning below** |

## Outputs

| File | What it is |
|------|-----------|
| `insights_report.md` | **Start here.** Recovery, strain flags, sleep regularity, training load, trends, associations |
| `llm_context.md` | Compact pre-analysed brief to paste into an LLM |
| `daily_metrics.csv` | One row per day, 60+ columns of raw daily aggregates |
| `daily_insights.csv` | Per-day baselines, deviations, z-scores, scores and flags |
| `weekly_summary.csv` | One row per ISO week |
| `workout_summary.csv` | One row per workout, with HR zones and cycling power |
| `metric_coverage.csv` | Per metric: first date, **reliable start**, coverage % |
| `data_quality_report.txt` | Wear stats, coverage, multi-device warnings, duplicate bursts |
| `available_types.csv` | Every Health type in the export with counts |
| `full_health_data.csv` | Legacy flat export (all records, one row each) |

Units are normalised to **kg, km, kcal, bpm, minutes, %**. Empty cells mean
*not measured* — never zero.

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
The 0.8–1.5 band is the commonly cited sweet spot.

**Associations** — lagged correlations between what you do and what your body
does next (sleep → next-day HRV, steps → next-night deep sleep, and so on).
Reported with `n` and a significance marker; relationships with fewer than 30
paired days are omitted rather than reported weakly.

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

## Sample data

```bash
mkdir -p /tmp/fx && cp fixtures/sample_export.xml /tmp/fx/export.xml
python3 convert_health_data.py --data-dir /tmp/fx
```

## Layout

| Module | Role |
|--------|------|
| `health_metrics.py` | Declarative metric registry — units, aggregation, grouping |
| `health_coaching.py` | Parsing, daily/weekly/workout aggregation, de-duplication, coverage |
| `health_insights.py` | Baselines, z-scores, scores, flags, trends, associations, reports |
| `convert_health_data.py` | CLI entry point, CDA and ECG handling |

## Privacy

Your export stays on your machine; the tool makes no network calls. `.gitignore`
excludes `export.xml`, `export_cda.xml`, `apple_health_export/`, `*.zip` and all
generated CSVs — health data should never enter git history, where every version
lives forever.

## Not medical advice

Every baseline here is personal, not a population norm, and every flag is a
prompt to pay attention rather than a diagnosis. Anything that concerns you is a
reason to talk to a clinician.
