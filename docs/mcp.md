# Claude Code, other MCP clients and local models

The Claude Desktop extension is the easy way in. This page is for everything
else: running the server by hand, other clients, and local models.

## Setup

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

## Letting the server build its own data

Instead of running the converter and pointing `--data-dir` at the result, give
the server the folder your exports land in:

```bash
python3 health_mcp.py --export ~/Downloads
```

This is what the Claude Desktop extension runs. It answers immediately, reads
the newest Apple Health export in that folder in the background, checking what
each zip holds rather than its name, and says "still reading" until the first
build is done. A newer export, or a changed `profile.md`, is picked up on a later
call, and the new build is swapped in whole, never mid-answer. It keeps the build,
the profile and the suggestions log in the platform's app-data folder (`--home`
puts them elsewhere). `--check` waits for the build and reports on it.

## What one of them actually returns

The tools that make this different from a metrics viewer are the ones that
decide what is worth your attention. `health_strain_episodes`, run against the
synthetic demo export above:

```
## Physiological strain episodes (most severe first)

18 episode(s) across the record; flagged days within 2 clear days of each other
are one episode. A flagged day is one where two or more of wrist temperature,
resting HR, HRV, respiratory rate and SpO2 moved the wrong way against the
personal 60-day baseline together. Sorted by flagged days rather than by date,
because one of these is usually far more consequential than the rest and date
order hides it.

| episode                | span days | flagged days | peak signals | signals                                             |
|------------------------|-----------|--------------|--------------|-----------------------------------------------------|
| 2024-11-18..2024-11-24 | 7         | 7            | 5            | wrist temp, resting HR, HRV, respiratory rate, SpO2 |
| 2026-02-10..2026-02-15 | 6         | 6            | 3            | wrist temp, HRV, respiratory rate, resting HR, SpO2 |
| 2025-11-12..2025-11-16 | 5         | 3            | 2            | HRV, SpO2, resting HR                               |
```

Nothing there is a diagnosis, and the tool does not offer one — it says which
days are unusual *for you* and which signals agreed, and leaves the conclusion
to you. The threshold is your own 60-day baseline, so it means something
different for every person who runs it.

The server reads the generated CSVs, never `export.xml` — parsing a gigabyte of
XML takes ~40 seconds, which no client will wait for on every call. It reports
how old the data is, so the model knows when it is answering from a stale export.

## Other clients, and local models

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
