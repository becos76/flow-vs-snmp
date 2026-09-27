"""Streamlit entrypoint. Ties together kentik/client.py (fetch) and analysis.py
(join + deltas), and renders the two charts. See CLAUDE.md's "UI (phase 1)" section
for the design this follows."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# Running this file directly (via `streamlit run app/app.py`) puts app/'s own
# directory on sys.path, not its parent — so the `app` package below can't be found
# from inside itself without this. Must run before the `from app...` imports.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.analysis import AGGREGATIONS, aggregate_hourly, analyze
from app.config import SUSPECT_THRESHOLD_PCT, TIMESPAN, TIMESPANS
from app.kentik.client import fetch_all
from app.kentik.exceptions import (
    CredentialsRejected,
    KentikError,
    NoComparisonDataError,
    NoDataError,
    ShapeError,
    UpstreamError,
    UpstreamTimeout,
)

st.set_page_config(page_title="Flow vs. SNMP", layout="wide", initial_sidebar_state="expanded")
st.title("Flow vs. SNMP")

ERROR_MESSAGES = {
    CredentialsRejected: "Credentials rejected.",
    NoDataError: "No data for this device/interface/timespan.",
    UpstreamTimeout: "Kentik did not respond in time.",
    UpstreamError: "Kentik query failed.",
    ShapeError: "Kentik query failed.",
    NoComparisonDataError: "Flow succeeded, but both SNMP and NMS failed — nothing to compare against.",
}

PAIR_OPTIONS = {
    "Flow vs SNMP": "flow_vs_snmp_de_pct",
    "Flow vs NMS": "flow_vs_nms_pct",
    "SNMP vs NMS": "snmp_paths_pct",
}

OUTPUT_DIR = REPO_ROOT / "outputs"

BPS_COLUMNS = ("flow", "snmp_de", "snmp_nms")
PCT_COLUMNS = ("flow_vs_snmp_de_pct", "flow_vs_nms_pct", "snmp_paths_pct")

TABLE_VISIBLE_ROWS = 100
TABLE_ROW_HEIGHT_PX = 35
TABLE_HEADER_HEIGHT_PX = 38

JSON_CODE_HEIGHT_PX = 500  # approximates 20 visible lines before internal scroll kicks in


def _pick_bps_unit(peak: float) -> tuple[str, float]:
    """Decimal SI scaling (1000-based, matching network bandwidth convention, not
    binary/1024-based). One unit for the whole traffic table — chosen from the peak
    across flow/snmp_de/snmp_nms together, so all three stay directly comparable at a
    glance instead of each column (or each cell) picking its own scale."""
    if pd.isna(peak) or peak < 1e3:
        return "bit", 1.0
    if peak < 1e6:
        return "Kbit", 1e3
    if peak < 1e9:
        return "Mbit", 1e6
    return "Gbit", 1e9


def _save_capture(capture: dict, *, stamp: str, device_id: str, ifindex: str) -> None:
    """Writes each captured request/response to its own file, named
    `{stamp}-{device_id}-{ifindex}-{request|response}-{flow|snmp|nms}.json`.
    `capture` only holds keys for sources that got at least as far as building a payload —
    see fetch_all's docstring for why a failed call can still leave partial entries."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    for key, payload in capture.items():
        source, kind = key.split("_", 1)
        filename = f"{stamp}-{device_id}-{ifindex}-{kind}-{source}.json"
        (OUTPUT_DIR / filename).write_text(json.dumps(payload, indent=2))


# Offset-detectable comparison sources: (series key, display label, checkbox state key).
OFFSET_SOURCES = (
    ("snmp_de", "SNMP", "shift_snmp_de"),
    ("snmp_nms", "NMS", "shift_snmp_nms"),
)


def _offset_minutes(offset_buckets: int, bucket_seconds: int) -> int:
    return offset_buckets * bucket_seconds // 60


def _signed(minutes: int) -> str:
    """"+5" / "−5" — a typographic minus, so it doesn't read as a hyphen in labels."""
    return f"{minutes:+d}".replace("-", "−")


def _shift_suffix(key: str, meta: dict) -> str:
    """"−5 min" when a timestamp shift is applied to this source, else "" — appended to
    its trace and table header so a shifted series is never mistaken for the data as
    Kentik returned it."""
    applied = meta["offsets"].get(key, {}).get("applied_buckets", 0)
    if not applied:
        return ""
    return f"{_signed(_offset_minutes(applied, meta['bucket_seconds']))} min"


def _display_table(df: pd.DataFrame, meta: dict, extra_columns: list[str]) -> pd.DataFrame:
    """Display-only copy for st.dataframe: ts as UTC datetime, the three bps columns in one
    shared unit named in the header (plus any applied shift), pct columns rounded.
    Never touches the cached/analyzed DataFrame itself."""
    table = df.assign(ts=pd.to_datetime(df["ts"], unit="ms", utc=True))[
        ["ts", *BPS_COLUMNS, *PCT_COLUMNS, *extra_columns]
    ].copy()
    peak = table[list(BPS_COLUMNS)].abs().max().max()
    unit_label, divisor = _pick_bps_unit(peak)
    for column in BPS_COLUMNS:
        table[column] = (table[column] / divisor).round(2)
    table[list(PCT_COLUMNS)] = table[list(PCT_COLUMNS)].round(2)
    return table.rename(columns={
        c: f"{c} ({', '.join(filter(None, (unit_label, _shift_suffix(c, meta))))})"
        for c in BPS_COLUMNS
    })


def _collapse_to_one_xaxis(fig: go.Figure, n_rows: int) -> None:
    """Collapse every row onto the single x-axis `x`. make_subplots' shared_xaxes
    only *links* separate axes (x, x2, x3 via "matches"), and Plotly sizes an "across"
    spike line to the y-axes sharing its x-axis id — so with linked axes the guide line
    stopped at the hovered subplot's edges. With one axis it spans every row, while the
    unified tooltip (default hoversubplots="single") still lists only the hovered row's
    traces. Cost: time tick labels only render under the bottom row. Call after every
    update_xaxes/row= call, since those recreate xaxis2/xaxis3 from the subplot grid."""
    fig.update_traces(xaxis="x")
    for n in range(2, n_rows + 1):
        fig.layout[f"xaxis{n}"] = None
        fig.layout[f"yaxis{n}"].anchor = "x"
    fig.layout.xaxis.update(
        anchor=f"y{n_rows}", matches=None, showticklabels=True, title_text="Time (UTC)",
    )


STATUS_SOURCES = (
    ("flow", "Flow", "flow", "datapoints"),
    ("snmp_de", "SNMP", "snmp", "datapoints"),
    ("snmp_nms", "NMS", "nms", "datapoints"),
    ("fps", "FPS", "fps", "datapoints"),
    ("sample_rate", "Sample Rate", "rate", "distinct rates"),
)


with st.sidebar:
    st.header("Kentik credentials")
    email = st.text_input("Kentik email")
    api_token = st.text_input("API token", type="password")
    cluster = st.selectbox("Cluster", ["US", "EU"])
    save_files = st.checkbox(
        "Save files", help=f"Save each query's request/response JSON under {OUTPUT_DIR.name}/"
    )

st.session_state.setdefault("form_expanded", True)


def _collapse_form():
    st.session_state["form_expanded"] = False


# key= + on_change="rerun" makes this a controlled widget: st.session_state["form_expanded"]
# drives its expanded/collapsed state on every rerun, including reruns after the user has
# manually toggled it. key= alone does nothing here — Streamlit only binds an expander's
# state to session_state when on_change tracks it; with the default on_change="ignore" the
# expanded= value is just a one-time initializer that's ignored on every later rerun.
#
# Collapsing happens via the submit button's on_click, not by writing to
# st.session_state["form_expanded"] later in this script — a keyed widget's state can't be
# written after that widget has already been instantiated in the same run (Streamlit raises
# StreamlitWidgetAlreadyInstantiatedError), and the expander above is drawn before we'd know
# whether the analyze call below succeeds. on_click callbacks run before the rerun that
# redraws the expander, which is the ordering Streamlit requires.
with st.expander(
    "Device / Interface", expanded=st.session_state["form_expanded"],
    key="form_expanded", on_change="rerun",
):
    with st.form("query_form"):
        device_id = st.text_input("Device ID")
        ifindex = st.text_input("Interface index (ifindex)")
        st.caption("Timespan: last 24 hours (fixed) 5min Datapoints (fixed)")
        submitted = st.form_submit_button("Analyze", on_click=_collapse_form)

if submitted:
    field_errors = []
    if not email:
        field_errors.append("Email is required.")
    if not api_token:
        field_errors.append("API token is required.")
    if not device_id.isdigit():
        field_errors.append("Device ID must be numeric.")
    if not ifindex.isdigit():
        field_errors.append("Interface index must be numeric.")

    if field_errors:
        for message in field_errors:
            st.error(message)
    else:
        timespan_config = TIMESPANS[TIMESPAN]
        # Always captured in memory (for the raw-JSON popovers below), regardless
        # of the "Save files" checkbox — that checkbox only controls whether this also gets
        # written to outputs/ on disk.
        capture = {}
        with st.spinner("Querying Kentik..."):
            try:
                flow, snmp_de, snmp_nms, fps, sample_rate_breakdown, warnings = asyncio.run(
                    fetch_all(
                        email=email,
                        api_token=api_token,
                        cluster=cluster,
                        device_id=int(device_id),
                        ifindex=int(ifindex),
                        lookback_seconds=timespan_config["lookback_seconds"],
                        bucket_seconds=timespan_config["bucket_seconds"],
                        capture=capture,
                    )
                )
            except KentikError as exc:
                st.error(ERROR_MESSAGES.get(type(exc), "Kentik query failed."))
                st.session_state.pop("fetched", None)
            else:
                # Cache the parsed series, not the analyzed result: analyze() is re-run on
                # every rerun below (cheap — 288 rows) so the offset checkboxes can re-align
                # a source without re-firing any Kentik call.
                st.session_state["fetched"] = dict(
                    series=(flow, snmp_de, snmp_nms, fps, sample_rate_breakdown),
                    device_id=int(device_id), ifindex=int(ifindex),
                    bucket_seconds=timespan_config["bucket_seconds"], warnings=warnings,
                )
                # A new fetch starts back at the data as returned — a shift opted into for
                # the previous interface must not silently carry over.
                for _key, _label, state_key in OFFSET_SOURCES:
                    st.session_state.pop(state_key, None)
            finally:
                if capture:
                    st.session_state["capture"] = capture
                    if save_files:
                        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                        _save_capture(capture, stamp=stamp, device_id=device_id, ifindex=ifindex)
                        st.toast(f"Saved query files to {OUTPUT_DIR.name}/ ({stamp})")

if "fetched" in st.session_state:
    fetched = st.session_state["fetched"]

    # First pass on the data exactly as returned, to learn which sources have a proposed
    # offset; the checkbox state (read before its widget is drawn below — allowed, since
    # it's keyed) decides whether to apply it. Detection itself never depends on what's
    # applied, so the proposal and its checkbox stay stable across reruns.
    def _run_analyze(offsets: dict[str, int]) -> tuple[dict, pd.DataFrame]:
        return analyze(
            *fetched["series"],
            device_id=fetched["device_id"], ifindex=fetched["ifindex"],
            timespan=TIMESPAN, bucket_seconds=fetched["bucket_seconds"],
            warnings=fetched["warnings"],
            snmp_de_offset_buckets=offsets.get("snmp_de", 0),
            snmp_nms_offset_buckets=offsets.get("snmp_nms", 0),
        )

    meta, rows = _run_analyze({})
    applied = {
        key: meta["offsets"][key]["proposed_buckets"]
        for key, _label, state_key in OFFSET_SOURCES
        if key in meta["offsets"] and st.session_state.get(state_key, False)
    }
    if any(applied.values()):
        meta, rows = _run_analyze(applied)

    for warning in meta["warnings"]:
        st.warning(warning)

    # Proposed timestamp offsets (see detect_offset in analysis.py): one opt-in checkbox
    # per source where a shift was detected, off by default so the charts show the data
    # as Kentik returned it unless the user chooses otherwise. The label carries the whole
    # finding, rather than a separate notice + checkbox pair, to save vertical space.
    for key, label, state_key in OFFSET_SOURCES:
        detection = meta["offsets"].get(key)
        if not detection or not detection["proposed_buckets"]:
            continue
        minutes = _offset_minutes(detection["proposed_buckets"], meta["bucket_seconds"])
        direction = "late" if minutes < 0 else "early"
        st.checkbox(
            f"{label} looks {abs(minutes)} min {direction} vs Flow — "
            f"shift {label} {_signed(minutes)} min",
            key=state_key,
            help=(
                f"Bucket-to-bucket changes in {label} line up with Flow at "
                f"{detection['score_best']:.2f} when shifted {_signed(minutes)} min, vs "
                f"{detection['score_zero']:.2f} as returned (1.0 = lockstep). Shifting "
                "re-aligns the timestamps before every delta is computed; the raw JSON "
                "stays as Kentik returned it."
            ),
        )

    # Status per source, derived from meta["counts"] rather than tracked separately —
    # a count of 0 only happens when that source was skipped after a degrade (flow itself
    # is required and always > 0 here, see the partial-failure policy in CLAUDE.md).
    #
    # Each badge is itself the "View JSON" trigger (an st.popover, not a separate
    # st.success/error + button pair) to save vertical space. It reads from
    # st.session_state["capture"], populated on every fetch attempt regardless of the "Save
    # files" checkbox (see the submit block above) — so it keeps working across reruns (e.g.
    # switching Chart/Table) without re-fetching. A source's response can be missing even
    # though its request is present (a degraded comparison call that built its payload but
    # then failed) — handled inline as "Not available." rather than disabling the popover.
    capture = st.session_state.get("capture", {})
    status_cols = st.columns(len(STATUS_SOURCES))
    for col, (meta_key, label, capture_key, unit) in zip(status_cols, STATUS_SOURCES, strict=True):
        count = meta["counts"][meta_key]
        request_payload = capture.get(f"{capture_key}_request")
        response_payload = capture.get(f"{capture_key}_response")
        icon = "✅" if count > 0 else "❌"
        status_text = f"{count} {unit}" if count > 0 else "unavailable"
        # NMS has no /query/url equivalent — it's a topxdata-only endpoint (see
        # fetch_all's _fetch_de_portal_url) — so only the four DE-based popovers get a
        # third tab.
        has_portal_url = capture_key != "nms"
        portal_url = capture.get(f"{capture_key}_portal_url") if has_portal_url else None
        with col:
            with st.popover(f"{icon} {label}: {status_text}", use_container_width=True):
                tab_names = ["Request", "Response"] + (["Portal Link"] if has_portal_url else [])
                tabs = st.tabs(tab_names)
                tab_request, tab_response = tabs[0], tabs[1]
                with tab_request:
                    if request_payload is not None:
                        st.code(
                            json.dumps(request_payload, indent=2), language="json",
                            height=JSON_CODE_HEIGHT_PX,
                        )
                    else:
                        st.caption("Not available.")
                with tab_response:
                    if response_payload is not None:
                        st.code(
                            json.dumps(response_payload, indent=2), language="json",
                            height=JSON_CODE_HEIGHT_PX,
                        )
                    else:
                        st.caption("Not available.")
                if has_portal_url:
                    with tabs[2]:
                        if portal_url is not None:
                            st.markdown(f"[{portal_url}]({portal_url})")
                        else:
                            st.caption("Not available.")

    ts = pd.to_datetime(rows["ts"], unit="ms", utc=True)

    view = st.radio("View", ["Chart", "Table"], horizontal=True)

    if view == "Table":
        # All three pct columns at once, unlike the chart's one-pair-at-a-time selector —
        # a table has no reason to hide the other two.
        st.dataframe(
            _display_table(rows, meta, ["aligned"]), use_container_width=True, hide_index=True,
            height=TABLE_HEADER_HEIGHT_PX + TABLE_VISIBLE_ROWS * TABLE_ROW_HEIGHT_PX,
        )
    else:
        pair_label = st.selectbox("Comparison", list(PAIR_OPTIONS.keys()), index=0)
        pct_col = PAIR_OPTIONS[pair_label]
        suspect = rows[pct_col] > SUSPECT_THRESHOLD_PCT

        # Same shared-unit scaling as the Table view — one unit for all three
        # traffic lines, chosen from their peak together, so they stay directly comparable.
        # Display-only: divides a plotted copy, never touches the cached `rows` DataFrame.
        peak = rows[list(BPS_COLUMNS)].abs().max().max()
        unit_label, divisor = _pick_bps_unit(peak)

        # The sampling-rate diagnostic is best-effort (FPS/sample-rate breakdown
        # degrade independently, see fetch_all) — only add the 3rd row when FPS actually
        # came back, rather than rendering a permanently-empty panel.
        rate_columns = meta["sample_rate_columns"]
        has_sampling_data = meta["counts"]["fps"] > 0
        n_rows = 3 if has_sampling_data else 2

        # One figure with stacked subplots, not independent st.plotly_chart calls per
        # chart — every row ends up on the same x-axis (see _collapse_to_one_xaxis), so zooming/
        # panning any subplot updates all of them, client-side, with no rerun.
        titles = [f"Traffic ({unit_label})", f"{pair_label} (%)"]
        row_heights = [0.6, 0.4]
        if has_sampling_data:
            titles.append("Sampling rate diagnostic (flows/s)")
            row_heights = [0.45, 0.3, 0.25]
        fig = make_subplots(
            rows=n_rows, cols=1, shared_xaxes=True, vertical_spacing=0.12,
            row_heights=row_heights, subplot_titles=tuple(titles),
        )

        for column, base_label in (("flow", "Flow"), ("snmp_de", "SNMP"), ("snmp_nms", "NMS")):
            suffix = _shift_suffix(column, meta)
            label = f"{base_label} ({suffix})" if suffix else base_label
            fig.add_trace(
                go.Scatter(
                    x=ts, y=rows[column] / divisor, name=label, mode="lines", connectgaps=False,
                    hovertemplate=f"%{{y:.2f}} {unit_label}<extra>{label}</extra>",
                ),
                row=1, col=1,
            )

        fig.add_trace(
            go.Scatter(
                x=ts, y=rows[pct_col], name=pair_label, mode="lines",
                fill="tozeroy", line_color="steelblue", connectgaps=False,
                legend="legend2", hovertemplate=f"%{{y:.2f}}%<extra>{pair_label}</extra>",
            ),
            row=2, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=ts[suspect], y=rows.loc[suspect, pct_col],
                name=f"> {SUSPECT_THRESHOLD_PCT:.0f}%", mode="markers",
                marker=dict(color="red", size=8),
                legend="legend2",
                hovertemplate=f"%{{y:.2f}}%<extra>&gt; {SUSPECT_THRESHOLD_PCT:.0f}%</extra>",
            ),
            row=2, col=1,
        )
        # Explicit shape rather than add_hline(row=2): add_hline would anchor it to
        # xaxis2, which no longer exists once every row shares the one x-axis (see below).
        fig.add_shape(
            type="line", xref="x domain", x0=0, x1=1, yref="y2",
            y0=SUSPECT_THRESHOLD_PCT, y1=SUSPECT_THRESHOLD_PCT,
            line=dict(dash="dot", color="red"),
        )

        if has_sampling_data:
            # One stacked colored band per distinct active sample rate (flows/s,
            # zero-filled only here for display — analysis.py keeps NaN for "not active
            # this bucket"), with the FPS line drawn on top. A single flat band the whole
            # day means one constant rate; multiple simultaneous bands is the downsampling
            # signal itself, so no separate count/highlight logic is needed — matches how
            # Kentik's own UI renders this (confirmed against a live screenshot).
            #
            # Each rate is split into two traces: the visible stacked band (hoverinfo=
            # "skip" — up to 40 of these all reporting "0.00 flows/s" at once would flood
            # the unified tooltip at every timestamp) and an invisible, legend-less
            # companion whose zero points are NaN so it only surfaces in the hover
            # tooltip for the buckets where that rate was actually active. This also
            # keeps legend3 down to just the "FPS" entry — 40 individual rate entries
            # were tall enough to visually spill over legend2's box above it.
            for column in rate_columns:
                rate_label = column.removeprefix("rate_")
                stacked_values = rows[column].fillna(0)
                fig.add_trace(
                    go.Scatter(
                        x=ts, y=stacked_values, mode="lines",
                        stackgroup="sample_rate", line=dict(width=0.5),
                        showlegend=False, hoverinfo="skip",
                    ),
                    row=3, col=1,
                )
                fig.add_trace(
                    go.Scatter(
                        x=ts, y=stacked_values.replace(0, float("nan")), mode="none",
                        showlegend=False,
                        hovertemplate=f"%{{y:.2f}} flows/s<extra>Rate {rate_label}</extra>",
                    ),
                    row=3, col=1,
                )
            fig.add_trace(
                go.Scatter(
                    x=ts, y=rows["fps"], name="FPS", mode="lines",
                    line_color="royalblue", connectgaps=False, legend="legend3",
                    hovertemplate="%{y:.2f} flows/s<extra>FPS</extra>",
                ),
                row=3, col=1,
            )

        # theme=None below is load-bearing: Streamlit's default "streamlit" theme restyles
        # charts and drops gridlines regardless of the figure's own layout. Major ticks
        # (labels) stay on Plotly's "auto" mode — recomputed client-side as the user
        # zooms/pans, so labels never overcrowd. Minor gridlines are pinned to
        # bucket_seconds instead, so every actual 5-min datapoint gets its own gridline
        # regardless of zoom, without a label on each one.
        #
        # showspikes/spikemode="across" draws the dotted guide line at the hovered
        # timestamp. hoverformat controls the unified tooltip's header date, separately
        # from tickformat above.
        fig.update_xaxes(
            tickformat="%H:%M\n%b %d", showgrid=True, hoverformat="%H:%M %b %d",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikedash="dot", spikethickness=1, spikecolor="gray",
            minor=dict(
                showgrid=True, dtick=meta["bucket_seconds"] * 1000,
                gridwidth=1, gridcolor="rgba(128, 128, 128, 0.35)",
            ),
        )
        fig.update_yaxes(title_text=f"{unit_label}/s", row=1, col=1)
        fig.update_yaxes(title_text="% difference", row=2, col=1)
        if has_sampling_data:
            fig.update_yaxes(title_text="flows/s", row=3, col=1)

        _collapse_to_one_xaxis(fig, n_rows)

        # One legend per subplot, each vertically aligned with its own chart — traces are
        # split between them via each trace's `legend="legendN"` above.
        row2_top = fig.layout.yaxis2.domain[1]
        layout_legends = {
            "legend": dict(title="Traffic", y=1, yanchor="top"),
            "legend2": dict(title=pair_label, y=row2_top, yanchor="top"),
        }
        if has_sampling_data:
            row3_top = fig.layout.yaxis3.domain[1]
            layout_legends["legend3"] = dict(title="Sampling", y=row3_top, yanchor="top")
        fig.update_layout(
            **layout_legends,
            height=900 if has_sampling_data else 700,
            hovermode="x unified",
        )

        st.plotly_chart(fig, use_container_width=True, theme=None)

    # Phase 2 — Aggregation: 24 one-hour rollups of the same 5-min rows plotted above
    # (so any applied SNMP/NMS shift carries through), computed locally from the cache —
    # no Kentik call. Follows the main View toggle (Chart/Table) and, in Chart view, the
    # main comparison-pair selector, rather than adding its own copies of either. See
    # aggregate_hourly in analysis.py for block boundaries and the delta convention.
    st.subheader("Aggregation")
    agg_label = st.radio(
        "Aggregation function", list(AGGREGATIONS), horizontal=True, key="agg_func",
        help="Each point rolls up one hour (12 × 5-min datapoints), counted back from the "
             "newest datapoint. Deltas compare the aggregated values.",
    )
    hourly = aggregate_hourly(rows, agg_label, meta["bucket_seconds"])

    if view == "Table":
        st.dataframe(
            _display_table(
                hourly.assign(ts_end=pd.to_datetime(hourly["ts_end"], unit="ms", utc=True)),
                meta, ["ts_end", "flow_points", "snmp_de_points", "snmp_nms_points"],
            ),
            use_container_width=True, hide_index=True,
            height=TABLE_HEADER_HEIGHT_PX + len(hourly) * TABLE_ROW_HEIGHT_PX,
        )
    else:
        agg_ts = pd.to_datetime(hourly["ts"], unit="ms", utc=True)
        agg_peak = hourly[list(BPS_COLUMNS)].abs().max().max()
        agg_unit, agg_divisor = _pick_bps_unit(agg_peak)
        agg_suspect = hourly[pct_col] > SUSPECT_THRESHOLD_PCT

        agg_fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12, row_heights=[0.6, 0.4],
            subplot_titles=(f"Hourly {agg_label} traffic ({agg_unit})", f"{pair_label} (%)"),
        )
        for column, base_label in (("flow", "Flow"), ("snmp_de", "SNMP"), ("snmp_nms", "NMS")):
            suffix = _shift_suffix(column, meta)
            label = f"{base_label} ({suffix})" if suffix else base_label
            agg_fig.add_trace(
                go.Scatter(
                    x=agg_ts, y=hourly[column] / agg_divisor, name=label,
                    mode="lines+markers", connectgaps=False,
                    customdata=hourly[f"{column}_points"],
                    hovertemplate=(
                        f"%{{y:.2f}} {agg_unit} (%{{customdata}} pts)<extra>{label}</extra>"
                    ),
                ),
                row=1, col=1,
            )
        agg_fig.add_trace(
            go.Scatter(
                x=agg_ts, y=hourly[pct_col], name=pair_label, mode="lines+markers",
                fill="tozeroy", line_color="steelblue", connectgaps=False,
                legend="legend2", hovertemplate=f"%{{y:.2f}}%<extra>{pair_label}</extra>",
            ),
            row=2, col=1,
        )
        agg_fig.add_trace(
            go.Scatter(
                x=agg_ts[agg_suspect], y=hourly.loc[agg_suspect, pct_col],
                name=f"> {SUSPECT_THRESHOLD_PCT:.0f}%", mode="markers",
                marker=dict(color="red", size=8), legend="legend2",
                hovertemplate=f"%{{y:.2f}}%<extra>&gt; {SUSPECT_THRESHOLD_PCT:.0f}%</extra>",
            ),
            row=2, col=1,
        )
        agg_fig.add_shape(
            type="line", xref="x domain", x0=0, x1=1, yref="y2",
            y0=SUSPECT_THRESHOLD_PCT, y1=SUSPECT_THRESHOLD_PCT,
            line=dict(dash="dot", color="red"),
        )
        # Same axis treatment as the main chart; x is each block's first 5-min timestamp.
        agg_fig.update_xaxes(
            tickformat="%H:%M\n%b %d", showgrid=True, hoverformat="%H:%M %b %d",
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikedash="dot", spikethickness=1, spikecolor="gray",
        )
        agg_fig.update_yaxes(title_text=f"{agg_unit}/s", row=1, col=1)
        agg_fig.update_yaxes(title_text="% difference", row=2, col=1)
        _collapse_to_one_xaxis(agg_fig, 2)
        agg_fig.update_layout(
            legend=dict(title="Traffic", y=1, yanchor="top"),
            legend2=dict(title=pair_label, y=agg_fig.layout.yaxis2.domain[1], yanchor="top"),
            height=600, hovermode="x unified",
        )
        st.plotly_chart(agg_fig, use_container_width=True, theme=None)
