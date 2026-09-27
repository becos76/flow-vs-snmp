import pandas as pd
import pytest

from app.analysis import aggregate_hourly, analyze, detect_offset, shift_series
from app.kentik.parse import parse_de_sample_rate_breakdown, parse_de_series, parse_nms_series


@pytest.fixture
def fixture_series(de_flow_response, de_snmp_response, me_nms_response):
    return (
        parse_de_series(de_flow_response),
        parse_de_series(de_snmp_response),
        parse_nms_series(me_nms_response),
    )


def test_source_point_counts(fixture_series):
    flow, snmp_de, snmp_nms = fixture_series
    assert len(flow) == len(snmp_de) == len(snmp_nms) == 288


def test_outer_join_counts(fixture_series):
    flow, snmp_de, snmp_nms = fixture_series
    meta, rows = analyze(
        flow, snmp_de, snmp_nms,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )
    assert meta["counts"] == {
        "flow": 288, "snmp_de": 288, "snmp_nms": 288, "fps": 0, "sample_rate": 0,
        "rows": 290, "aligned": 286, "unaligned": 4,
    }
    assert meta["sample_rate_columns"] == []
    assert len(rows) == 290
    assert rows["aligned"].sum() == 286
    assert meta["window"] == {"start_ms": 1767225300000, "end_ms": 1767312000000}


def test_flow_vs_snmp_de_delta_stats(fixture_series):
    flow, snmp_de, snmp_nms = fixture_series
    _, rows = analyze(
        flow, snmp_de, snmp_nms,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )
    delta = rows["flow_vs_snmp_de_pct"].dropna()
    assert delta.mean() == pytest.approx(7.68, abs=0.05)
    assert delta.median() == pytest.approx(7.30, abs=0.05)
    assert (delta >= 0).all()


def test_snmp_paths_worst_outlier(fixture_series):
    flow, snmp_de, snmp_nms = fixture_series
    _, rows = analyze(
        flow, snmp_de, snmp_nms,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )
    worst = rows.loc[rows["ts"] == 1767267900000, "snmp_paths_pct"].iloc[0]
    assert worst == pytest.approx(40.0, abs=0.05)
    assert rows["snmp_paths_pct"].max() == pytest.approx(40.0, abs=0.05)
    assert (rows["snmp_paths_pct"] > 20).sum() == 3


def test_nms_degraded_nulls_the_control_columns_but_keeps_flow_vs_snmp(fixture_series):
    flow, snmp_de, _ = fixture_series
    meta, rows = analyze(
        flow, snmp_de, None,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
        warnings=["NMS unavailable"],
    )
    assert meta["counts"]["snmp_nms"] == 0
    assert meta["warnings"] == ["NMS unavailable"]
    assert rows["snmp_nms"].isna().all()
    assert rows["flow_vs_nms_pct"].isna().all()
    assert rows["snmp_paths_pct"].isna().all()
    # flow and snmp_de share identical timestamps, so nothing is unaligned here.
    assert rows["aligned"].all()
    assert rows["flow_vs_snmp_de_pct"].notna().all()


def test_fully_absent_source_columns_stay_float_not_object(fixture_series):
    # Regression: when a source is entirely absent, every value in its column
    # (and its derived pct columns) used to be Python `None`, which makes pandas infer
    # `object` dtype instead of `float64` since there's no float anywhere in the column
    # to anchor the inference. That silently broke numpy-level ops downstream in the UI
    # (e.g. `.abs()` on the table's traffic columns raised TypeError on real None values,
    # even though pandas' own `/`  and `.round()` tolerate None). Columns must stay
    # `float64` with NaN, per CLAUDE.md's documented data contract.
    flow, snmp_de, _ = fixture_series
    _, rows = analyze(
        flow, snmp_de, None,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
        warnings=["NMS unavailable"],
    )
    for column in ("snmp_nms", "flow_vs_nms_pct", "snmp_paths_pct"):
        assert rows[column].dtype == "float64"
    # This is exactly the op that crashed pre-fix on an all-None object column.
    rows[["flow", "snmp_de", "snmp_nms"]].abs()


def test_fps_and_sample_rate_breakdown_columns(fixture_series, de_fps_response, de_sampling_rate_response):
    # Only exercises that the fps/rate columns land correctly alongside the other sources.
    flow, snmp_de, snmp_nms = fixture_series
    fps = parse_de_series(de_fps_response)
    breakdown = parse_de_sample_rate_breakdown(de_sampling_rate_response)

    meta, rows = analyze(
        flow, snmp_de, snmp_nms, fps, breakdown,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )

    assert meta["counts"]["fps"] == 288
    assert meta["counts"]["sample_rate"] == 40
    assert len(meta["sample_rate_columns"]) == 40
    assert "rate_2000" in meta["sample_rate_columns"]

    # fps/rate timestamps outside the flow/snmp/nms union still widen `rows` via the
    # outer join, same as any other source. Each rate column carries a real 0.0 (not NaN)
    # for buckets where that rate wasn't active — Kentik's points already cover all 288
    # timestamps per row — so "active" means nonzero, not notna.
    assert rows["fps"].notna().sum() == 288
    assert rows["rate_2000"].notna().sum() == 288
    assert (rows["rate_2000"].fillna(0) != 0).sum() == 91


def test_snmp_de_degraded_nulls_that_column_but_keeps_flow_vs_nms(fixture_series):
    flow, _, snmp_nms = fixture_series
    meta, rows = analyze(
        flow, None, snmp_nms,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
        warnings=["SNMP (DE) control unavailable"],
    )
    assert meta["counts"]["snmp_de"] == 0
    assert meta["warnings"] == ["SNMP (DE) control unavailable"]
    assert rows["snmp_de"].isna().all()
    assert rows["flow_vs_snmp_de_pct"].isna().all()
    assert rows["snmp_paths_pct"].isna().all()
    assert rows["flow_vs_nms_pct"].notna().any()


def _spiky_series(n=288, start_ms=1767225300000, step_ms=300_000):
    # Deterministic, non-smooth traffic so bucket-to-bucket changes carry the signal.
    values = [1e6 + ((i * 7919) % 97) * 1e4 for i in range(n)]
    return {start_ms + i * step_ms: v for i, v in enumerate(values)}


def test_detect_offset_on_fixture_proposes_no_shift(fixture_series):
    flow, snmp_de, snmp_nms = fixture_series
    for other in (snmp_de, snmp_nms):
        assert detect_offset(flow, other, 300)["proposed_buckets"] == 0


def test_detect_offset_finds_one_bucket_late_source():
    flow = _spiky_series()
    late = shift_series(flow, 1, 300)  # same traffic, stamped one bucket late
    result = detect_offset(flow, late, 300)
    assert result["proposed_buckets"] == -1
    assert result["score_best"] == pytest.approx(1.0)
    assert result["score_zero"] < 0.5


def test_detect_offset_too_few_points_proposes_nothing():
    flow = _spiky_series(n=5)
    result = detect_offset(flow, shift_series(flow, 1, 300), 300)
    assert result == {"proposed_buckets": 0, "score_best": None, "score_zero": None}


def test_analyze_applies_offset_and_detection_stays_on_raw_data():
    flow = _spiky_series()
    late = shift_series(flow, 1, 300)
    meta, rows = analyze(
        flow, late, None,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
        snmp_de_offset_buckets=-1,
    )
    assert meta["offsets"]["snmp_de"]["proposed_buckets"] == -1
    assert meta["offsets"]["snmp_de"]["applied_buckets"] == -1
    assert "snmp_nms" not in meta["offsets"]
    assert (rows["flow_vs_snmp_de_pct"].dropna() == 0).all()
    assert meta["counts"]["aligned"] == 288


def test_analyze_without_offset_keeps_data_as_returned():
    flow = _spiky_series()
    meta, rows = analyze(
        flow, shift_series(flow, 1, 300), None,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )
    assert meta["offsets"]["snmp_de"]["applied_buckets"] == 0
    assert meta["counts"]["rows"] == 289  # one edge bucket each side unaligned
    assert rows["flow_vs_snmp_de_pct"].dropna().gt(0).any()


def _ramp_rows(n=288, start_ms=1767225300000, step_ms=300_000):
    flow = {start_ms + i * step_ms: float(i) for i in range(n)}
    _, rows = analyze(
        flow, dict(flow), None,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )
    return rows


def test_aggregate_hourly_fixture_has_24_full_blocks(fixture_series):
    flow, snmp_de, snmp_nms = fixture_series
    _, rows = analyze(
        flow, snmp_de, snmp_nms,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
    )
    hourly = aggregate_hourly(rows, "P95", 300)
    assert len(hourly) == 24
    assert (hourly["flow_points"] == 12).all()
    assert (hourly["snmp_de_points"] == 12).all()
    # NMS's two points before the newest-anchored 24h are dropped, and its window ends
    # two buckets before DE's, so the newest block holds only 10 NMS points.
    assert hourly["snmp_nms_points"].sum() == 286
    assert hourly["snmp_nms_points"].iloc[-1] == 10
    assert hourly["ts"].is_monotonic_increasing
    assert hourly["ts_end"].iloc[-1] == rows["ts"].max()
    assert (hourly["ts_end"] - hourly["ts"] == 3_300_000).all()


@pytest.mark.parametrize("func, expected", [
    ("Min", 276.0), ("Max", 287.0), ("Average", 281.5), ("P95", 286.45),
])
def test_aggregate_hourly_functions_on_newest_block(func, expected):
    hourly = aggregate_hourly(_ramp_rows(), func, 300)
    assert hourly["flow"].iloc[-1] == pytest.approx(expected)
    assert hourly["flow_vs_snmp_de_pct"].iloc[-1] == 0


def test_aggregate_hourly_empty_block_stays_nan_with_zero_points():
    rows = _ramp_rows()
    rows = rows[(rows.index < 12) | (rows.index >= 24)]  # drop the 2nd hour entirely
    hourly = aggregate_hourly(rows, "Max", 300)
    assert len(hourly) == 24
    assert hourly["flow_points"].iloc[1] == 0
    assert pd.isna(hourly["flow"].iloc[1])
    assert pd.isna(hourly["flow_vs_snmp_de_pct"].iloc[1])


def test_aggregate_hourly_uses_shifted_rows():
    flow = _spiky_series()
    late = shift_series(flow, 1, 300)
    _, rows = analyze(
        flow, late, None,
        device_id=1, ifindex=1, timespan="1day", bucket_seconds=300,
        snmp_de_offset_buckets=-1,
    )
    hourly = aggregate_hourly(rows, "P95", 300)
    assert (hourly["flow_vs_snmp_de_pct"] == 0).all()
