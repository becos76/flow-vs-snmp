"""Turns a raw Kentik response into dict[ts_ms, bps] and nothing more — no joining,
no deltas, no aggregates. See CLAUDE.md's "Gotchas verified against the fixtures" for
why the validation gates here look the way they do."""

from .exceptions import NoDataError, ShapeError


def parse_de_series(response: dict) -> dict[int, float]:
    """Parses a Data Explorer response (shared by both the flow and SNMP calls)."""
    data = response.get("results", [{}])[0].get("data", [])
    if len(data) == 0:
        raise NoDataError("no data for this device/interface/timespan")
    if len(data) != 1:
        raise ShapeError(f"expected exactly one data row, got {len(data)}")

    time_series = data[0].get("timeSeries") or {}
    if len(time_series) != 1:
        raise ShapeError(f"expected exactly one key under timeSeries, got {len(time_series)}")
    (sub_series,) = time_series.values()

    if len(sub_series) != 1:
        raise ShapeError(f"expected exactly one key under the timeSeries sub-series, got {len(sub_series)}")
    (points,) = sub_series.values()

    return {int(ts): float(bps) for ts, bps, *_rest in points}


def parse_de_sample_rate_breakdown(response: dict) -> dict[str, dict[int, float]]:
    """Parses the `dimension: ["sample_rate"]` breakdown: one row per distinct
    sample-rate value seen that day, not one row per interface, so unlike parse_de_series
    this deliberately does not gate on len(data) == 1. Kentik only attaches a populated
    timeSeries to the top-`topx` rows by flows/sec; the rest are scalar-only aggregates
    and carry no per-datapoint values, so they're skipped rather than raising."""
    data = response.get("results", [{}])[0].get("data", [])
    if len(data) == 0:
        raise NoDataError("no data for this device/interface/timespan")

    breakdown: dict[str, dict[int, float]] = {}
    for row in data:
        time_series = row.get("timeSeries") or {}
        if not time_series:
            continue
        if len(time_series) != 1:
            raise ShapeError(f"expected exactly one key under timeSeries, got {len(time_series)}")
        (sub_series,) = time_series.values()

        if len(sub_series) != 1:
            raise ShapeError(
                f"expected exactly one key under the timeSeries sub-series, got {len(sub_series)}"
            )
        (points,) = sub_series.values()

        rate_label = str(row.get("sample_rate", row.get("key")))
        breakdown[rate_label] = {int(ts): float(fps) for ts, fps, *_rest in points}

    return breakdown


def parse_nms_series(response: dict) -> dict[int, float]:
    """Parses a Metrics Explorer (NMS) response."""
    data = response.get("data", [])
    if len(data) == 0:
        raise NoDataError("no data for this device/interface/timespan")
    if len(data) != 1:
        raise ShapeError(f"expected exactly one data row, got {len(data)}")

    points = data[0].get("timeseries")
    if points is None:
        raise ShapeError("NMS response missing 'timeseries'")

    return {int(ts): float(bps) for ts, bps in points}
