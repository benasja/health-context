# Apple Health XML to CSV Converter

Turns an Apple Health export (`export.xml`) into analysis-ready CSVs **and** a
computed insights report — baselines, deviations, trends and personal
associations, rather than a pile of rows to eyeball.

Python 3.10+. **No dependencies** — standard library only.

## Quick start

1. Health app → profile picture → **Export All Health Data**. AirDrop or copy
   the zip to your computer.
2. Point the tool at it. **No unzipping** — hand it the zip exactly as it arrived:

```bash
python3 convert_health_data.py --data-dir ~/Downloads/export.zip --out-dir output
```

`--data-dir` accepts the `.zip`, an unzipped `apple_health_export/` folder, or
any folder containing either — point it at `~/Downloads` and it picks the newest
export it finds. Only the files actually parsed are extracted (~1 GB of the
~1.4 GB export is route GPX and a clinical-document mirror that is never read),
and the extraction is cached until you export again.

A full run is around two minutes, with a progress bar during the parse.

### Installing

Nothing to install — clone and run, on macOS, Linux or Windows. Python 3.10+,
zero dependencies.

If you would rather have the commands on your `PATH`:

```bash
pip install git+https://github.com/benasja/xml_csv_converter_applehealth
health-convert --data-dir ~/Downloads/export.zip --out-dir ~/health
```

That gives you `health-convert` and `health-mcp`. Still no runtime dependencies —
packaging only adds the entry points.

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
| `health_history.py` | Long memory — records, capacity gap, eras, streaks, strain episodes, distributions |
| `convert_health_data.py` | CLI entry point, CDA and ECG handling |

## Ask questions instead of reading files (MCP server)

Rather than pasting `llm_context.md` into a chat, let the assistant query your
data directly. `health_mcp.py` is an [MCP](https://modelcontextprotocol.io)
server exposing 12 tools over stdio — still zero dependencies, still entirely
local, nothing uploaded anywhere.

```
"How did I sleep last week compared with my best month?"
"What were my ten biggest training days ever?"
"Was I ill in November? What do the vitals say?"
"Compare my exercise this month against January 2025."
```

### Setup

**1.** Generate the data once (repeat whenever you re-export):

```bash
python3 convert_health_data.py --data-dir apple_health_export --out-dir output
```

**2.** Check it loads:

```bash
python3 health_mcp.py --data-dir output --check
```

**3.** Register it. For **Claude Code**:

```bash
claude mcp add apple-health -- python3 "$PWD/health_mcp.py" --data-dir "$PWD/output"
```

For **Claude Desktop** or any other MCP client, print the config block and paste
it into the client's `mcpServers` object:

```bash
python3 health_mcp.py --data-dir output --print-config
```

Claude Desktop keeps that file at
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.
Restart the client afterwards.

### Tools

| Tool | Answers |
|------|---------|
| `health_overview` | Where things stand now vs what you have sustained before |
| `health_list_metrics` | What is available and how completely it is covered |
| `health_metric_stats` | Summary of one metric, and where it sits in your history |
| `health_compare_periods` | Two date ranges, head to head |
| `health_top_days` | Best or worst days, dated |
| `health_day_detail` | Everything about one specific day |
| `health_sleep` | Duration, stages, efficiency, timing |
| `health_workouts` | Sessions, or totals by activity type |
| `health_strain_episodes` | Multi-signal physiological flags, grouped |
| `health_weekly` | Week-by-week rollup |
| `health_context_pack` | The whole briefing |
| `health_data_quality` | Wear rates, coverage, excluded artifacts |

The server reads the generated CSVs, never `export.xml` — parsing a gigabyte of
XML takes ~40 seconds, which no client will wait for on every call. It reports
how old the data is, so the model knows when it is answering from a stale export.

### Other clients, and local models

MCP is an open protocol, so this is not Claude-specific — anything that speaks
MCP over stdio works. That includes editor assistants (Cursor, Zed, Continue,
Cline, VS Code) and clients that can drive a **local** model, which is the
interesting case for health data: nothing ever leaves the machine, not even to
an API.

The reason a modest local model can do useful work here is that it never has to
crunch anything. The 2.1M records, the baselines, the percentiles and the
capacity comparisons are all computed in Python beforehand; the model reads a
small table and reasons about it. That is a very different task from handing a
7B model a year of CSV and hoping.

Two things matter when running locally:

**Context window.** A single uncapped tool result can swallow an 8K window
before the model answers anything. Cap it:

```bash
python3 health_mcp.py --data-dir output --max-output-chars 6000
```

`health_context_pack` also takes a `section` argument — called bare it returns a
~300-token index of the 15 sections, so the model fetches only what it needs
instead of a 6,500-token wall. Any result that does get cut says so explicitly,
including a warning not to treat the last visible row as the last that exists.

**Tool-calling quality.** The server assumes a model that reads tool
descriptions and picks sensibly. Very small models tend to guess metric names
instead of calling `health_list_metrics` first, then report "no data" for a
metric that exists under another name. Anything competent at function calling
handles this; the smallest models will need the metric name given to them.

## Tell it what the data cannot know

The export records what your body did. It has no idea *why*. Without that,
anything reading it fills the gap with a guess — and the usual guess is that a
drop in training means you lost motivation, when it might have been surgery.

Copy the template and write three honest lines:

```bash
cp profile.example.md profile.md
```

```markdown
## Constraints
- TFCC surgery Nov 2025. No loaded wrist extension yet.

## Context for the data
- The training drop from Oct 2025 is medical, not motivational.
- I am NOT trying to return to January 2025 volume.
```

It is picked up automatically, placed at the very top of the context pack ahead
of every number, and exposed as the `health_profile` MCP tool. The pack tells the
reader to believe it over anything inferred from the data. With no profile, it
says so explicitly and instructs the reader not to guess.

`profile.md` is gitignored. Only the template ships.

## Keeping it up to date automatically

**Apple provides no way to trigger an export programmatically** — no API, no
Shortcuts action, no iCloud sync of the export file. "Export All Health Data" is
a manual tap, and nothing here can change that. (Third-party HealthKit apps that
hold Apple's entitlements can write exports on a schedule; that is the only
automated source.)

Everything *after* the export can be automated, and the manual part reduces to:
export, AirDrop to your Downloads folder, done.

Point the scheduler at the folder your exports land in:

```bash
python3 convert_health_data.py --data-dir ~/Downloads --out-dir ~/health --print-schedule
```

That prints a ready-to-install monthly job for your OS — a launchd plist on
macOS, a systemd service and timer on Linux, a `schtasks` command on Windows —
with real paths already filled in. Save or run it as the comment says.

From then on it rebuilds monthly from the newest export in that folder. The MCP
server re-reads on every call, so it needs no restart, and if you forget to
export it tells the model the data is stale rather than quietly answering from
old numbers.

## Development

```bash
python3 test_health.py                  # 157 tests, stdlib only
pip install ruff && ruff check .
```

Every test corresponds to a bug that actually shipped and produced a plausible
*wrong number* rather than an error — percent metrics reported as fractions,
sleep nights split across calendar days, an isolated sensor artifact setting max
HR for a whole history, basal energy marking unworn days as worn. That is the
failure mode worth defending against: a pipeline that crashes tells you it is
broken, one that emits confident nonsense does not.

CI runs the tests on Python 3.10–3.13 (plus macOS), lints, executes the fixture
end-to-end, asserts every output file is non-empty, and **fails the build if any
health-data file is ever tracked by git**.

## Privacy

Your export stays on your machine; the tool makes no network calls. `.gitignore`
excludes `export.xml`, `export_cda.xml`, `apple_health_export/`,
`electrocardiograms/`, `workout-routes/`, `*.zip` and all generated CSVs —
health data should never enter git history, where every version lives forever.
CI enforces this, so a fork cannot quietly regress it.

## Limitations

**Validated against one person's export.** Every fix so far was driven by a
single ~2M-record dataset from an Apple Watch + iPhone + Wahoo trainer. Other
devices, apps and locales will exercise paths that have never run. Bug reports
describing the shape of your data are genuinely useful.

**Scores are judgement, not instruments.** The recovery weights, the load-band
thresholds, the 90-minute sleep-session gap, the 21 min/day load floor — all
documented at their definitions, none derived from research on you. Component
breakdowns are always emitted so a number can be audited rather than trusted.

**Eras are bucketing, not change-point detection.** The timeline is split by a
smoothed load signal crossing fixed thresholds. Boundaries can therefore land
close to an edge, and the output prints how close.

## Not medical advice

Every baseline here is personal, not a population norm, and every flag is a
prompt to pay attention rather than a diagnosis. Anything that concerns you is a
reason to talk to a clinician.
