# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working agreement

**Explain before coding.** The user wants to discuss every point of the design before implementation.
Do not scaffold, write, or refactor code without first describing the intended change and getting
agreement. Design discussion is the default mode; writing code is the exception that must be asked
for.

## Repository state

Phase 1 complete; phase 2 (sampling-rate diagnostic, Aggregation) implemented. Released as a Docker
image, not a Python package.

- `app/` — `app.py` (Streamlit entrypoint), `analysis.py`, `config.py`, `kentik/` (client, parse,
  payloads, exceptions, `templates/` — the verbatim query payloads).
- `tests/` — pytest suite; `tests/fixtures/` holds **synthetic** Kentik responses written by
  `tests/fixtures/generate.py` (deterministic). Never commit real captures: they carry a customer
  network's device IDs and traffic. `outputs/` (Save-files captures) is gitignored for that reason.
- `pyproject.toml` — pytest and ruff config only. Dependencies are pinned in `requirements*.txt`.
- `.streamlit/config.toml` — telemetry off, headless.
- `.github/workflows/` — `ci.yml` (ruff, pytest, Docker build + health check on push/PR) and
  `release.yml` (on tag `vX.Y.Z`: test, multi-arch image to `ghcr.io/<owner>/<repo>`, GitHub
  Release). The git tag is the only source of the version.

Commands:

```sh
pip install -r requirements-dev.txt
pytest                         # tests
ruff check .                   # lint
streamlit run app/app.py       # run locally
docker build -t flow-vs-snmp . && docker run --rm -p 8501:8501 flow-vs-snmp
python tests/fixtures/generate.py   # regenerate synthetic fixtures (then update test expectations)
```

## What the app does

Compare three independent bit-rate timeseries for one router interface over a timespan and quantify
the divergence between them. Flow-derived bps is expected to disagree with SNMP; the app's job is to
measure that gap, not to treat it as a bug.

## Agreed design decisions (phase 1)

| Decision | Choice |
| --- | --- |
| Stack | **Single Streamlit app** (Python) — no separate frontend service |
| Credentials | Entered in the UI per session, held in `st.session_state` only |
| Comparison | All three pairwise deltas, with DE-SNMP vs NMS as the data-quality control |
| Outliers | Always plotted; red-highlighted client-side above a 20% threshold on whichever comparison pair is currently selected — no backend suspect flag |
| Timespan | **Fixed to 1 day** for phase 1 — do not build the timespan selector yet |
| Aggregates | **None in the per-datapoint view.** Hourly rollups live only in the separate phase-2 Aggregation section — see "No aggregates" below |
| Direction | **Ingress only** (`input_port`). No egress toggle — not planned, not stubbed |
| Sampling rate | Not in phase 1 — added in phase 2 as a diagnostic subplot (see "Phase 2") |

Streamlit chosen over a FastAPI+React split to collapse the app to one service for a single-user,
locally-run diagnostic tool — one Docker container, no API boundary to design or version, no CORS.
pandas/scipy stay directly available for the phase-2 sampling-rate correlation work either way; with
Streamlit there's no HTTP contract to carry them across, so `analysis.py` can return a DataFrame
straight into the render code.

### Delta convention

Fixed once, used everywhere: `abs(a - b) / max(a, b) * 100`. Unsigned, always in `[0, 100)` — there
is no fixed reference side; whichever of the two values is larger at that datapoint is the
denominator. This applies identically to all three pairs (`flow_vs_snmp_de_pct`, `flow_vs_nms_pct`,
`snmp_paths_pct`), and the 20% highlight threshold means the same thing regardless of which source
happens to read higher. On the fixture's worst outlier (`snmp_de=1,200,000`, `snmp_nms=2,000,000`,
NMS is larger) this gives 40.0%, not the 66.7% you'd get by always dividing by DE-SNMP.

### No aggregates

Scope: this rule governs `analyze()`'s `(meta, rows)` and the main per-datapoint chart/table. The
phase-2 **Aggregation** section (see "Phase 2") is a deliberate, separate exception — it rolls the
same rows up hourly via `aggregate_hourly`, and never replaces or feeds back into the 5-min rows.

The point of the app is the difference at **every 5-minute datapoint**, not any number that collapses
those datapoints into one. So the response model carries no aggregates of any kind: no per-source
avg/p95/max bps, and no mean/median/p95 of the deltas either. All 288 datapoints stay intact and are
compared bucket-by-bucket.

**This is a response-model rule, not a request rule.** Keep sending `aggregates`, `aggregateTypes`,
`aggregateThresholds`, and `outsort` in the DE payloads and `rollups` + `sort` in the NMS payload —
those fields are load-bearing for the query, since `outsort`/`sort` reference the aggregate names.
Stripping them is a likely 400. Kentik will keep computing and returning avg/p95/max; the parser just
never reads those fields. Request verbatim, response minimal.

Likewise keep the NMS template's `limit: 5` / `includeTimeseries: 5`. With a correct filter it returns
one row anyway, and if the filter is ever wrong, returning up to 5 rows is exactly what the
`len(data) == 1` validation gate is there to catch. Setting it to 1 would mask that failure.

### Data contract

No HTTP layer, so there's no response body to design — `analyze(...)` in `analysis.py` returns
`(meta: dict, rows: pd.DataFrame)` straight to the Streamlit script. Same shape as an API response
would have had, just not serialized. Because rollups are gone, a per-source `Series` object would
hold only a list of points, so there is no separate series structure — one DataFrame serves both
charts.

```python
meta = {
    "device_id": 1, "ifindex": 1, "timespan": "1day", "bucket_seconds": 300,
    "window": {"start_ms": 1767225300000, "end_ms": 1767312000000},
    "counts": {"flow": 288, "snmp_de": 288, "snmp_nms": 288,
               "rows": 290, "aligned": 286, "unaligned": 4},
    "warnings": [],
}
# rows: one row per ts, columns —
# ts, flow, snmp_de, snmp_nms, flow_vs_snmp_de_pct, flow_vs_nms_pct, snmp_paths_pct, aligned
```

`rows` is an **outer join** over the union of timestamps, ordered by `ts`, with nullable values (`NaN`
in the DataFrame). Every delta is null unless both its operands are present, so the aligned/unaligned
distinction lives in the data rather than in prose, and the traffic chart can still plot the edge
buckets — Plotly leaves a gap for `NaN` by default, so the outer join's nulls produce a visual gap
rather than a misleading interpolated line. `meta["counts"]` is deliberately kept — it is bookkeeping
about data completeness, not an aggregate of bps, and without it the unaligned buckets would vanish
silently.

There is no `suspect` field and no threshold in `meta`. The 20% red-highlight threshold is a UI
rendering constant, applied when drawing whichever of the three pct columns is currently selected —
it has no meaning in `analysis.py` and doesn't affect what gets computed or returned.

Layer responsibilities: `parse.py` returns `dict[ts, bps]` per source and nothing more;
`analysis.py` does the outer join and the three deltas, returning `(meta, rows)` directly; `app.py`
(the Streamlit entrypoint) calls the Kentik client, calls `analysis.py`, and renders — there is no
router layer, since there's nothing to route.

### Timestamp offset detection

Some devices return DE-SNMP stamped exactly one bucket (5 min) late relative to flow — on the
clearest case measured, changes correlated 0.84 with SNMP shifted −5 min vs −0.17 as returned; seen so far
only on devices with no NMS data, while every NMS-monitored interface checked was aligned. Left
alone, every delta on such an interface compares two different 5-min windows.

`detect_offset` in `analysis.py` correlates bucket-to-bucket *changes* (first differences, on a
regular grid, joined on ts) between flow and each comparison source at shifts of ±3 buckets.
Changes, not levels — a smooth diurnal curve correlates highly with itself at any small shift.
A shift is proposed only when clear-cut: best score ≥ 0.5 and ≥ 0.3 above the unshifted score
(`OFFSET_MIN_SCORE`/`OFFSET_MIN_MARGIN`). It runs inside `analyze()` on the series as returned,
so the proposal never depends on what's applied, and lands in `meta["offsets"]` as
`{"snmp_de": {"proposed_buckets", "score_best", "score_zero", "applied_buckets"}, ...}` (present
sources only) — diagnostic bookkeeping like `counts`, not an aggregate of bps or deltas.

Applying is **opt-in, off by default** — measure, don't silently correct. `analyze()` takes
`snmp_de_offset_buckets`/`snmp_nms_offset_buckets` and shifts that source's timestamps before the
join. The UI shows one checkbox per source with a proposed shift (none otherwise — no manual
offset control), reset on every new Analyze. When applied, the trace and table header carry the
shift (`SNMP (−5 min)`); the raw-JSON popovers always show Kentik's data unshifted.

### Partial failure policy

Only flow is required: if it fails, the whole request fails. DE-SNMP and NMS each degrade
independently — if one of them fails or times out, return the rows with that source and its derived
columns null, and add a `warnings` entry saying so. If **both** DE-SNMP and NMS fail, there's nothing
left to compare flow against, so that combination aborts the request (`NoComparisonDataError`)
instead of rendering a flow-only chart.

### Retries, timeouts, and error handling

Kentik's Query API rate limits (per customer): max 4 concurrent requests; soft limit 30/min (past
that, Kentik just delays the response ~1s, no error); hard limit 100/min → `429`; 1500/hour. No
`Retry-After` header — backoff is self-paced, not server-hinted. One `/api/analyze` call fires 3
concurrent upstream calls (flow, DE-SNMP, NMS), comfortably under the 4-concurrent cap, so no
semaphore/queueing is needed for a single request — this is what makes the submit button staying
disabled while in flight (see "UI") load-bearing: it's what keeps concurrent clicks from stacking
toward that cap, not just a UX nicety.

**Retryable:** timeout, connection error, `429`, `5xx` — all transient. **Not retryable:** `401` (bad
creds won't fix themselves — retrying just hammers the API with a token that's already rejected) and
other `4xx` (deterministic — a malformed payload fails identically every time).

**Budgets differ by call, matching the partial-failure policy:**

- **Flow (required):** 2 retries (3 attempts), ~30s timeout per attempt, exponential backoff with
  jitter (~1s → 2s → 4s). Worst case ~93s — acceptable for a low-frequency, single-user tool where
  giving up too early on a slow-but-real Kentik response is the worse failure mode. Because the
  three calls run concurrently, total request latency is bounded by the slowest call, not the sum of
  all three.
- **DE-SNMP (comparison, optional):** same budget as flow — 2 retries, ~30s timeout — since it's the
  preferred comparison source and worth the same effort, even though its failure now degrades
  instead of aborting (unless NMS has also failed, see "Partial failure policy").
- **NMS (control, optional):** 1 retry (2 attempts), ~15s timeout. Since NMS failure degrades
  gracefully rather than aborting, there's no reason to spend as much of the user's wall-clock time
  retrying it.

Implementation is a small hand-rolled retry helper around `httpx` — no `tenacity` dependency for
something this size.

**Error → exception type, caught in `app.py` and rendered via `st.error`/`st.warning`:**

| Condition | Exception | Rendered as |
| --- | --- | --- |
| `401` from Kentik (any call) | `CredentialsRejected` | `st.error`: "credentials rejected" |
| Empty/malformed `data` (any call) | `NoDataError` | `st.error`: "no data for this device/interface/timespan" |
| Retries exhausted, timeout (any call) | `UpstreamTimeout` | `st.error`: "Kentik did not respond in time" |
| Retries exhausted, 5xx (any call) | `UpstreamError` | `st.error`: "Kentik query failed" |
| Flow succeeded but DE-SNMP *and* NMS both failed | `NoComparisonDataError` | `st.error`: "nothing to compare against" |
| DE-SNMP failure alone (NMS still available) | *(not raised)* | `st.warning`, from a `meta["warnings"]` entry; results still render |
| NMS failure alone (DE-SNMP still available) | *(not raised)* | `st.warning`, from a `meta["warnings"]` entry; results still render |

These are plain exception classes raised by the Kentik client / `analysis.py`, not HTTP status codes —
`app.py` catches them around the analyze call and decides whether to block results (the first five)
or just add a warning banner (the last two). Only flow's own failure (any of the first four rows) or
the combined DE-SNMP+NMS failure blocks results; a single comparison source failing does not.

### Credential rules

Email/token/cluster live in `st.session_state` only — in-memory, scoped to the live browser session,
gone when the session ends or the container restarts. Never written to disk, never in `localStorage`
(Streamlit has no equivalent — this is a property to preserve, not a library default), never logged,
never echoed back in an error message. Require HTTPS anywhere beyond localhost.

## UI (phase 1)

One page, no routing, no client-side history of past queries. Built with Streamlit; charts with
Plotly (`st.plotly_chart`).

**Sidebar:** email, API token (masked via `st.text_input(type="password")`), cluster (US/EU), and a
"Save files" checkbox (off by default) — plain widgets in `st.sidebar`, outside the form, so the
credential group can be collapsed (Streamlit's native sidebar toggle) independently of the
device/interface fields. Credentials are still typed in each session and held in `st.session_state`
only, never persisted beyond it.

**Save files.** When the sidebar checkbox is on at submit time, each source's request payload
(always, since it's built before the network call) and response body (only if that call succeeded)
are written to `outputs/` at the repo root as
`{stamp}-{device_id}-{ifindex}-{request|response}-{flow|snmp|nms}.json`, one UTC
`YYYYMMDDTHHMMSS` stamp shared across all files from one Analyze click. `outputs/` is gitignored —
these are per-run captures, not source. Nothing here writes credentials: the captured payloads are
just query bodies (device/ifindex/lookback/etc.), since auth travels only in headers, never in the
JSON body — so this doesn't conflict with the "Credential rules" above. A failed required (flow)
call can still leave partial files on disk for whichever comparison sources got far enough to
build a payload or return a response before the failure — see `fetch_all`'s docstring in
`client.py`.

**Form:** device ID, ifindex, on the main page — typed in each session, held in `st.session_state`,
never persisted beyond it. No timespan control — show static "Last 24 hours" text near the form
instead of a selector, so the fixed window is visible but not editable. No threshold control either —
20% is a fixed rendering constant, so there is one less field between the user and a result.

**Submit:** one `st.form_submit_button`, and the Kentik calls (flow required, DE-SNMP and NMS
best-effort) only run on that click — not on every rerun. Streamlit reruns the whole script
top-to-bottom on *any* widget interaction, including just switching the comparison-pair selector
below or editing a sidebar field, so the fetched, parsed series must be cached in `st.session_state`
(`"fetched"`) after a successful fetch and read from there on subsequent reruns. `analyze()` itself
re-runs off that cache on every rerun (cheap at 288 rows), so the offset checkbox (see "Timestamp
offset detection") can re-align a source without any Kentik call. Without that cache,
changing the pair selector would silently re-fire all three Kentik calls on every click — wasteful,
and the kind of thing that quietly eats into the rate-limit
budget documented above. The button click fetches and stores; every other widget only reads.

**Results only render on success.** A flow failure (401, empty `data`, timeout, 5xx) blocks results
entirely and shows the mapped error message (see "Retries, timeouts, and error handling") in place of
the charts — there's nothing to show without flow. A DE-SNMP-and-NMS-both-failed combination blocks
results the same way, since there'd be nothing to compare flow against. A single comparison source
failing is different: results still render, with a non-blocking `st.warning` banner (from
`meta["warnings"]`) above the charts, and that source's column plus its derived delta columns simply
absent from the plotted lines for that request.

**Query status panel, doubling as the raw JSON viewer.** Above the charts (below the warning
banners, if any), a row of three `st.popover`s — one each for Flow, SNMP, NMS — is both the status
badge and the "view JSON" trigger in one widget, rather than a separate badge-plus-button pair, to
save vertical space. Each popover's label (e.g. "✅ Flow: 288 datapoints" / "❌ SNMP: unavailable")
is derived straight from `meta["counts"]`, not tracked separately: a count of 0 means that source was
skipped after a degrade (flow itself is required and always > 0 whenever results render at all). No
live per-query progress — the existing `st.spinner` during the fetch already covers in-flight
feedback, and the popovers only need to reflect the final `meta` that's already cached in
`st.session_state`, so they re-render for free on every rerun (e.g. switching the comparison-pair
selector) without re-fetching anything.

Opening a popover shows two `st.tabs` ("Request"/"Response") rather than stacking both in one
scroll, each holding an `st.code(..., language="json", height=JSON_CODE_HEIGHT_PX)` — a built-in
copy-to-clipboard icon (no download buttons needed) and a fixed ~20-visible-line height so a large
payload scrolls internally instead of stretching the popover to its full length. The content comes
from `st.session_state["capture"]`, populated on every fetch attempt regardless of the "Save files"
checkbox, which only additionally controls whether the same data is also written to `outputs/` on
disk (see "Save files" below). A source's response can be missing even though its request is present
(a degraded comparison call that built its payload but then failed the network call) — shown inline
as "Not available." rather than disabling the popover.

**View toggle: Chart or Table.** A `st.radio` above the charts switches the whole results area
between the two-subplot chart (default) and a plain `st.dataframe` of every row — `ts` (converted to
UTC datetime for readability), `flow`, `snmp_de`, `snmp_nms`, and all three pct columns at once
(`flow_vs_snmp_de_pct`, `flow_vs_nms_pct`, `snmp_paths_pct`), plus `aligned`. The table shows all
three deltas together rather than one-at-a-time like the chart's comparison-pair selector, since
there's no rendering cost to it and no reason to hide the other two. Pure re-render off the cached
`rows` in `st.session_state` — no Kentik call involved, same as switching the comparison pair.

Display-only formatting, applied to a copy of `rows` and never touching the cached DataFrame or
`analysis.py`'s raw values: `flow`/`snmp_de`/`snmp_nms` share one bit/Kbit/Mbit/Gbit unit (decimal
SI, 1000-based — matching network bandwidth convention, not binary), chosen once from the peak
across all three columns together and named in each column's header (e.g. `flow (Mbit)`) rather than
repeated per cell, so the three stay directly comparable at a glance; values are divided down and
rounded to 2 decimals. The three pct columns are likewise rounded to 2 decimals but stay numeric.
The table's height is fixed to fit ~100 rows (`TABLE_VISIBLE_ROWS` in `app.py`) before it starts
internally scrolling, rather than Streamlit's much shorter default.

- **Traffic chart:** three lines labelled **Flow**, **SNMP**, **NMS** — `flow`, `snmp_de`, and
  `snmp_nms` respectively (SNMP and NMS are two independent read-paths for the same physical counter,
  not two different metrics). All three share one axis (same unit, same magnitude — no secondary axis
  needed). Plotly leaves a gap for `NaN` by default, so the unaligned edge buckets gap instead of
  drawing a misleading line across missing data.
- **Delta chart:** below the traffic chart, as the second subplot of the same Plotly figure (via
  `make_subplots(shared_xaxes=True)`), not a separate `st.plotly_chart` call — so zooming or panning
  either subplot updates both in the browser, with no Streamlit rerun. Renders **one comparison pair at a
  time**, chosen via a selector: **Flow vs SNMP** (default, `flow_vs_snmp_de_pct`), **Flow vs NMS**
  (`flow_vs_nms_pct`), or **SNMP vs NMS** (`snmp_paths_pct`, the data-quality control pair). Rendered
  as a filled area chart of the selected pct column. Any point where `pct > 20` renders in a
  distinct color (red) — computed from the pct value already in the cached DataFrame, re-evaluated
  whenever the selected pair changes. No Kentik call involved: switching pairs is a pure re-render off
  the cached `rows` in `st.session_state`.

## The three API calls

| | Flow (Data Explorer) | SNMP (Data Explorer) | NMS (Metrics Explorer) |
| --- | --- | --- | --- |
| Endpoint | `/api/v5/query/topxdata` | `/api/v5/query/topxdata` | `/api/next/v5/query/nms` |
| Series path | `results[0].data[0].timeSeries.both_bits_per_sec.flow` | `results[0].data[0].timeSeries.int64_00_per_sec.flow` | `data[0].timeseries` |
| Point shape | `[ts_ms, bps, width_s]` | `[ts_ms, bps, width_s]` | `[ts_ms, bps]` |
| Rollup keys | `avg_`/`p95th_`/`max_bits_per_sec` | `avg_`/`p95th_`/`max_ktappprotocol__snmp__INT64_00` | `p95_`/`max_`/`last_in-bit-rate` |
| Interface filter field | `input_port` | `input_port` | `ifindex` |
| Timespan field | `lookback_seconds: 86400` | same | `range.lookback: "PT86400S"` |
| Bucket field | `minsPolling: 5` + `forceMinsPolling: true` | same | `window.size: 300` |

Flow and SNMP share the same Data Explorer endpoint and differ only by payload. The two DE payloads
are ~99% identical; the only real differences are `metric` (`bytes` vs
`ktappprotocol__snmp__INT64_00`), `aggregates[].column` (`f_sum_both_bytes` vs `f_sum_int64_00`), and
the matching `aggregateTypes`/`outsort` names.

Auth headers on every call: `X-CH-Auth-Email`, `X-CH-Auth-API-Token`. `KENTIK_CLUSTER` (`US`/`EU`)
selects the base URL.

### Payload templating

Keep the captured payloads **verbatim** as template files and substitute only these fields. The DE
schema is ~220 undocumented lines; hand-building a minimal payload is how you earn a 400.

- DE, under `queries[0].query`: `lookback_seconds`, `minsPolling` (= bucket_seconds / 60), and the two
  filter values (`i_device_id`, `input_port`).
- NMS, under `query`: `range.lookback` (`PT{n}S`), `window.size`, and its filter values
  (`i_device_id`, `ifindex`).

Leave everything else untouched, including the vestigial `from_to_lookback: 3600` — it is inert while
`starting_time` is null.

## Gotchas verified against real Kentik responses

**Join on timestamp, never by index.** The NMS series is shifted exactly two buckets (600 s) earlier
than the DE series: for the same 1-day query, NMS's window starts and ends 600 s before DE's.
Index-zipping compares points 600 s apart. Zero-shift is confirmed correct — on the capture this
was verified against, 271 of 286 aligned buckets matched DE-SNMP to within 0.1%.

**Bucket size must be pinned identically on both APIs.** DE's `reAggInterval: "auto"` and NMS's
explicit `window.size` are independent knobs that merely happened to agree at 300 s in the
captures the templates came from. If they diverge the comparison is meaningless. Phase-1 target mapping (only the 1day row is
in scope):

| TIMESPAN | lookback | bucket | points |
| --- | --- | --- | --- |
| 1hr | 3600 / `PT3600S` | 60 s | 60 |
| **1day** | **86400 / `PT86400S`** | **300 s** | **288** |
| 3days | 259200 / `PT259200S` | 900 s | 288 |
| 1week | 604800 / `PT604800S` | 3600 s | 168 |

**`.flow` is a sub-series label, not "netflow".** Both DE responses nest their points under a key
literally named `flow` — including the SNMP one. Don't key parsing logic off that name meaning flow
data. Rather than hardcoding `both_bits_per_sec`/`int64_00_per_sec`, assert exactly one key exists
under `timeSeries`, take it, then take the single key beneath it, and raise a clear shape error
otherwise.

**The flow query is only ingress-direction because of the filter.** It uses `both_bits_per_sec` /
`f_sum_both_bytes` (labelled "Sampled at Ingress + Egress") and reduces to in-direction solely
because of the `input_port` filter. Switching to `output_port` silently changes what the number
means — moot for phase 1 since ingress-only is the agreed, permanent-for-now scope, not a TBD.

**Validation gates that turn silent wrongness into errors.** DE must return exactly one `data` row
(guaranteed by `dimension: ["Traffic"]` plus the filters, named `"Total"`). NMS returns no `key` or
`name` at all, so `len(data) == 1` is the only proof the query hit a single interface. Map 401 to
"credentials rejected"; empty `data` to "no data for this device/interface/timespan".

## Test fixtures

`tests/fixtures/*_response.json` are **synthetic** (see `generate.py`), shaped like real responses
and planting the behaviours above. Request fixtures are the templates themselves
(`load_template`). Values the tests pin:

- 288 points in each of flow / DE-SNMP / NMS, clean 300 s step, no nulls; DE starts
  `1767225900000` (2026-01-01T00:05Z), NMS two buckets earlier.
- 290 timestamps in the union; 286 aligned, 4 not (2 NMS-only at the start, 2 DE-only at the end).
- DE-SNMP vs NMS: within 0.05% everywhere except 3 planted outliers above 20% (NMS larger), worst
  40.0% at ts `1767267900000` (1,200,000 vs 2,000,000 bps) — renders red on the SNMP-vs-NMS view.
- Flow vs DE-SNMP: mean delta 7.68%, median 7.30%; flow under-reports in all but 21 of 288 buckets
  — the directional sampling-like bias the app exists to surface. No timestamp offset (detection
  proposes 0 for both SNMP and NMS).
- Sample-rate breakdown: 50 rows, 40 with a timeSeries; rate `1000` active in every bucket, rate
  `2000` in 91.

Regenerating changes these values — update the assertions (and this list) together.

## Phase 2

### Aggregation

A section below the main results showing **24 hourly rollups** of the same 5-min `rows` (so an
applied SNMP/NMS shift carries through), computed locally by `aggregate_hourly` in `analysis.py` —
no Kentik call, and not Kentik's own rollups (a second, independently bucketed path is exactly the
misalignment problem offset detection exists for).

- **Blocks:** 24 consecutive 12-point blocks counted back from the newest row — not UTC clock hours,
  since the 1-day window's edges don't fall on the hour (clock hours would give 25 with partial
  edges). Rows older than the 24th block are dropped; an empty block stays as a NaN row with
  0 points. x is each block's first 5-min timestamp (`ts`), `ts_end` its last.
- **Function:** user-selectable radio — **P95 (default)**, Min, Max, Average. P95 is pandas'
  default linear-interpolated quantile over ≤12 points; may differ slightly from Kentik's portal.
- **Deltas:** difference *of the aggregates* — `pct(agg(flow), agg(snmp_de))`, same convention as
  the per-datapoint deltas — so the delta subplot always matches the two traffic lines above it.
  Each source is aggregated over its own non-null points; `{source}_points` carries the count
  (shown in the traffic tooltip and the table).
- **UI:** no controls of its own beyond the function radio — it follows the main View toggle
  (Chart → Traffic + delta subplot, same single-x-axis/spike/20%-red treatment as the main chart;
  Table → 24-row table with all three pct columns) and, in Chart view, the main comparison-pair
  selector.

### Sampling rate

Implemented as the third subplot of the main chart: flows/s plus one stacked band per distinct
applied sample rate (from a `dimension: ["sample_rate"]` breakdown, of which Kentik only populates
a timeSeries for the top 40 rows). A *varying* applied rate implies Kentik's license-cap
downsampling was active; a constant one implies it was not. Best-effort: the subplot is omitted if
FPS fails. Not yet done: correlating the applied rate with the size of the flow-vs-SNMP delta.
