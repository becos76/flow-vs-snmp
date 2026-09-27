"""Outer join over the three per-source series plus the three pairwise deltas. No
aggregates of any kind — see CLAUDE.md's "No aggregates" and "Data contract" sections.

Delta convention, fixed everywhere: unsigned percentage difference, always in [0, 100).
For "A_vs_B_pct": abs(A - B) / max(A, B) * 100 — whichever side happens to be larger at
that datapoint is the denominator. There is no fixed reference side and no sign; the
20% threshold applies the same regardless of which source is higher."""

import math

import pandas as pd

Series = dict[int, float]

NA = float("nan")

# Timestamp-offset detection. Some devices' DE-SNMP comes back stamped one bucket late
# relative to flow (seen on devices with no NMS data), which makes every delta compare
# two different 5-min windows. Detection correlates bucket-to-bucket *changes* (first
# differences) rather than levels — a smooth diurnal curve correlates highly with itself
# at any small shift, but its changes only line up at the true alignment. A shift is
# proposed only when it's clear-cut: best score >= OFFSET_MIN_SCORE and beating the
# unshifted score by >= OFFSET_MIN_MARGIN. Detection only ever *proposes*; the offset is
# applied only when the caller passes it back in (the UI's opt-in checkbox).
OFFSET_MAX_BUCKETS = 3
OFFSET_MIN_SCORE = 0.5
OFFSET_MIN_MARGIN = 0.3
OFFSET_MIN_POINTS = 10


def _pct(a: float, b: float) -> float:
    if math.isnan(a) or math.isnan(b):
        return NA
    denom = max(a, b)
    if denom == 0:
        return 0.0
    return abs(a - b) / denom * 100


def shift_series(series: Series, offset_buckets: int, bucket_seconds: int) -> Series:
    """Moves every timestamp by offset_buckets * bucket_seconds; negative = earlier."""
    delta_ms = offset_buckets * bucket_seconds * 1000
    return {ts + delta_ms: value for ts, value in series.items()}


def _change_correlation(
    flow: Series, other: Series, offset_buckets: int, bucket_seconds: int,
) -> float | None:
    """Pearson correlation of first differences, joined on timestamp after shifting
    `other`. Differences are taken on a regular bucket grid, so a missing bucket yields
    NaN for its neighbours instead of a difference spanning the gap."""
    step_ms = bucket_seconds * 1000
    frame = pd.DataFrame({
        "flow": pd.Series(flow, dtype=float),
        "other": pd.Series(shift_series(other, offset_buckets, bucket_seconds), dtype=float),
    })
    if frame.empty:
        return None
    grid = range(int(frame.index.min()), int(frame.index.max()) + step_ms, step_ms)
    changes = frame.reindex(grid).diff().dropna()
    if len(changes) < OFFSET_MIN_POINTS:
        return None
    if changes["flow"].std() == 0 or changes["other"].std() == 0:
        return None
    return float(changes["flow"].corr(changes["other"]))


def detect_offset(
    flow: Series, other: Series, bucket_seconds: int, max_buckets: int = OFFSET_MAX_BUCKETS,
) -> dict:
    """Proposes a whole-bucket timestamp shift for `other` relative to flow, or 0 when
    no shift clearly beats the data as returned. proposed_buckets is what to *add* to
    other's timestamps: -1 means other is stamped one bucket late."""
    scores = {
        k: _change_correlation(flow, other, k, bucket_seconds)
        for k in range(-max_buckets, max_buckets + 1)
    }
    score_zero = scores[0]
    valid = {k: v for k, v in scores.items() if v is not None}
    proposed = 0
    score_best = score_zero
    if valid:
        best_k = max(valid, key=valid.get)
        best = valid[best_k]
        baseline = score_zero if score_zero is not None else 0.0
        if best_k != 0 and best >= OFFSET_MIN_SCORE and best - baseline >= OFFSET_MIN_MARGIN:
            proposed, score_best = best_k, best
    return {"proposed_buckets": proposed, "score_best": score_best, "score_zero": score_zero}


def analyze(
    flow: Series,
    snmp_de: Series | None,
    snmp_nms: Series | None,
    fps: Series | None = None,
    sample_rate_breakdown: dict[str, Series] | None = None,
    *,
    device_id: int,
    ifindex: int,
    timespan: str,
    bucket_seconds: int,
    warnings: list[str] | None = None,
    snmp_de_offset_buckets: int = 0,
    snmp_nms_offset_buckets: int = 0,
) -> tuple[dict, pd.DataFrame]:
    de_present = snmp_de is not None
    nms_present = snmp_nms is not None

    # Detection always runs on the series exactly as Kentik returned them, so the
    # proposal doesn't change once the user opts into applying it.
    offsets = {}
    for key, series, applied in (
        ("snmp_de", snmp_de, snmp_de_offset_buckets),
        ("snmp_nms", snmp_nms, snmp_nms_offset_buckets),
    ):
        if series is None:
            continue
        offsets[key] = {**detect_offset(flow, series, bucket_seconds), "applied_buckets": applied}
    if de_present and snmp_de_offset_buckets:
        snmp_de = shift_series(snmp_de, snmp_de_offset_buckets, bucket_seconds)
    if nms_present and snmp_nms_offset_buckets:
        snmp_nms = shift_series(snmp_nms, snmp_nms_offset_buckets, bucket_seconds)

    fps_present = fps is not None
    rate_breakdown = sample_rate_breakdown or {}
    rate_columns = [f"rate_{label}" for label in rate_breakdown]

    sources = (
        [flow]
        + ([snmp_de] if de_present else [])
        + ([snmp_nms] if nms_present else [])
        + ([fps] if fps_present else [])
        + list(rate_breakdown.values())
    )

    union_ts = sorted(set().union(*sources))

    records = []
    aligned_count = 0
    for ts in union_ts:
        flow_val = flow.get(ts, NA)
        snmp_de_val = snmp_de.get(ts, NA) if de_present else NA
        snmp_nms_val = snmp_nms.get(ts, NA) if nms_present else NA
        fps_val = fps.get(ts, NA) if fps_present else NA

        aligned = (
            not math.isnan(flow_val)
            and (not math.isnan(snmp_de_val) if de_present else True)
            and (not math.isnan(snmp_nms_val) if nms_present else True)
        )
        if aligned:
            aligned_count += 1

        record = {
            "ts": ts,
            "flow": flow_val,
            "snmp_de": snmp_de_val,
            "snmp_nms": snmp_nms_val,
            "flow_vs_snmp_de_pct": _pct(flow_val, snmp_de_val),
            "flow_vs_nms_pct": _pct(flow_val, snmp_nms_val),
            "snmp_paths_pct": _pct(snmp_nms_val, snmp_de_val),
            "aligned": aligned,
            "fps": fps_val,
        }
        for label, series in rate_breakdown.items():
            record[f"rate_{label}"] = series.get(ts, NA)
        records.append(record)

    rows = pd.DataFrame.from_records(
        records,
        columns=[
            "ts", "flow", "snmp_de", "snmp_nms",
            "flow_vs_snmp_de_pct", "flow_vs_nms_pct", "snmp_paths_pct",
            "aligned", "fps", *rate_columns,
        ],
    )

    meta = {
        "device_id": device_id,
        "ifindex": ifindex,
        "timespan": timespan,
        "bucket_seconds": bucket_seconds,
        "window": {
            "start_ms": union_ts[0] if union_ts else None,
            "end_ms": union_ts[-1] if union_ts else None,
        },
        "counts": {
            "flow": len(flow),
            "snmp_de": len(snmp_de) if de_present else 0,
            "snmp_nms": len(snmp_nms) if nms_present else 0,
            "fps": len(fps) if fps_present else 0,
            "sample_rate": len(rate_columns),
            "rows": len(union_ts),
            "aligned": aligned_count,
            "unaligned": len(union_ts) - aligned_count,
        },
        "sample_rate_columns": rate_columns,
        "offsets": offsets,
        "warnings": warnings or [],
    }

    return meta, rows


# Aggregation (phase 2): hourly rollups of the 5-min rows, a separate, explicitly
# aggregated view — the per-datapoint rows above stay intact. Keys are the UI's labels.
AGGREGATIONS = {
    "P95": lambda s: s.quantile(0.95),
    "Min": lambda s: s.min(),
    "Max": lambda s: s.max(),
    "Average": lambda s: s.mean(),
}
AGGREGATE_BLOCKS = 24
AGGREGATE_BLOCK_SECONDS = 3600


def aggregate_hourly(rows: pd.DataFrame, func: str, bucket_seconds: int) -> pd.DataFrame:
    """Rolls `rows` (analyze()'s output, so any applied offset is already in it) up into
    AGGREGATE_BLOCKS consecutive one-hour blocks counted back from the newest row —
    always exactly 24 full blocks, not clock hours, since the 1-day window's edges don't
    fall on the hour. Each source is aggregated over its own non-null points; rows older
    than the 24th block are dropped. Blocks with no data stay as NaN rows (count 0).

    Deltas are the difference *of the aggregates* — pct(agg(flow), agg(snmp_de)) — with the
    same convention as the per-datapoint deltas, so the delta chart always matches the
    two traffic lines plotted above it.

    Returns one row per block, oldest first: ts / ts_end (first and last 5-min timestamp
    the block covers), flow, snmp_de, snmp_nms, {source}_points, and the three pct columns."""
    aggregate = AGGREGATIONS[func]
    block_ms = AGGREGATE_BLOCK_SECONDS * 1000
    bucket_ms = bucket_seconds * 1000
    newest = rows["ts"].max() if not rows.empty else 0
    block = (newest - rows["ts"]) // block_ms
    in_range = rows[block < AGGREGATE_BLOCKS]
    grouped = in_range.groupby(block[block < AGGREGATE_BLOCKS])

    out = pd.DataFrame(index=pd.RangeIndex(AGGREGATE_BLOCKS - 1, -1, -1))
    for column in ("flow", "snmp_de", "snmp_nms"):
        out[column] = grouped[column].agg(aggregate).astype(float)
        out[f"{column}_points"] = grouped[column].count()
    out = out.fillna({f"{c}_points": 0 for c in ("flow", "snmp_de", "snmp_nms")})
    for column in ("flow_points", "snmp_de_points", "snmp_nms_points"):
        out[column] = out[column].astype(int)
    out.insert(0, "ts_end", newest - out.index * block_ms)
    out.insert(0, "ts", out["ts_end"] - block_ms + bucket_ms)

    out["flow_vs_snmp_de_pct"] = [_pct(a, b) for a, b in zip(out["flow"], out["snmp_de"], strict=True)]
    out["flow_vs_nms_pct"] = [_pct(a, b) for a, b in zip(out["flow"], out["snmp_nms"], strict=True)]
    out["snmp_paths_pct"] = [_pct(a, b) for a, b in zip(out["snmp_nms"], out["snmp_de"], strict=True)]
    return out.reset_index(drop=True)
