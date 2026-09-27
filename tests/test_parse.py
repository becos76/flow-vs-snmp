import pytest

from app.kentik.exceptions import NoDataError, ShapeError
from app.kentik.parse import parse_de_sample_rate_breakdown, parse_de_series, parse_nms_series


def test_parse_de_flow(de_flow_response):
    series = parse_de_series(de_flow_response)
    assert len(series) == 288
    assert series[1767225900000] == pytest.approx(2053090.07)


def test_parse_de_snmp(de_snmp_response):
    series = parse_de_series(de_snmp_response)
    assert len(series) == 288
    assert series[1767225900000] == pytest.approx(2028658.98)


def test_parse_nms(me_nms_response):
    series = parse_nms_series(me_nms_response)
    assert len(series) == 288
    assert series[1767225900000] == pytest.approx(2028475.2)
    # NMS is shifted two buckets (600s) earlier than DE.
    assert min(series) == 1767225300000
    assert max(series) == 1767311400000


def test_de_empty_data_raises_no_data_error():
    with pytest.raises(NoDataError):
        parse_de_series({"results": [{"data": []}]})


def test_de_multiple_data_rows_raises_shape_error():
    row = {"timeSeries": {"x": {"flow": [[1, 2, 300]]}}}
    with pytest.raises(ShapeError):
        parse_de_series({"results": [{"data": [row, row]}]})


def test_de_multiple_keys_under_time_series_raises_shape_error():
    row = {"timeSeries": {"a": {"flow": [[1, 2, 300]]}, "b": {"flow": [[1, 2, 300]]}}}
    with pytest.raises(ShapeError):
        parse_de_series({"results": [{"data": [row]}]})


def test_nms_empty_data_raises_no_data_error():
    with pytest.raises(NoDataError):
        parse_nms_series({"data": []})


def test_nms_multiple_data_rows_raises_shape_error():
    row = {"timeseries": [[1, 2]]}
    with pytest.raises(ShapeError):
        parse_nms_series({"data": [row, row]})


def test_parse_de_fps(de_fps_response):
    # FPS reuses parse_de_series directly — same single-"Total"-row shape as flow/SNMP.
    series = parse_de_series(de_fps_response)
    assert len(series) == 288
    assert all(isinstance(v, float) for v in series.values())


def test_parse_de_sample_rate_breakdown(de_sampling_rate_response):
    # Kentik reports every distinct sample-rate value seen that day, but only attaches a
    # populated timeSeries to the top 40 by flows/sec (the payload's topx: 40) — the rest
    # are scalar-only aggregate rows and must be skipped, not raised on. The fixture has
    # 50 rows, 40 with a timeSeries.
    breakdown = parse_de_sample_rate_breakdown(de_sampling_rate_response)
    assert len(breakdown) == 40
    assert sum(1 for v in breakdown["2000"].values() if v != 0) == 91
    assert all(v != 0 for v in breakdown["1000"].values())


def test_de_sample_rate_breakdown_empty_data_raises_no_data_error():
    with pytest.raises(NoDataError):
        parse_de_sample_rate_breakdown({"results": [{"data": []}]})


def test_de_sample_rate_breakdown_skips_rows_without_timeseries():
    rows = [
        {"sample_rate": 100, "timeSeries": {}},
        {"sample_rate": 200, "timeSeries": {"flows_per_sec": {"flow": [[1, 2, 300]]}}},
    ]
    breakdown = parse_de_sample_rate_breakdown({"results": [{"data": rows}]})
    assert list(breakdown.keys()) == ["200"]
