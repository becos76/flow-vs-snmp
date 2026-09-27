import copy

from app.kentik.payloads import build_de_payload, build_nms_payload


def _filter_value(filters: list[dict], field: str) -> str:
    return next(f["filterValue"] for f in filters if f["filterField"] == field)


def test_build_de_payload_substitutes_fields_and_leaves_template_untouched(de_flow_request):
    original = copy.deepcopy(de_flow_request)

    payload = build_de_payload(
        de_flow_request, device_id=999, ifindex=42, lookback_seconds=3600, bucket_seconds=60
    )

    query = payload["queries"][0]["query"]
    assert query["lookback_seconds"] == 3600
    assert query["minsPolling"] == 1

    filters = query["filters"]["filterGroups"][0]["filters"]
    assert _filter_value(filters, "i_device_id") == "999"
    assert _filter_value(filters, "input_port") == "42"

    # aggregates/outsort/aggregateTypes survive verbatim — load-bearing for the query.
    assert payload["queries"][0]["query"]["outsort"] == "avg_bits_per_sec"
    assert de_flow_request == original


def test_build_nms_payload_substitutes_fields_and_leaves_template_untouched(me_nms_request):
    original = copy.deepcopy(me_nms_request)

    payload = build_nms_payload(
        me_nms_request, device_id=999, ifindex=42, lookback_seconds=3600, bucket_seconds=60
    )

    query = payload["query"]
    assert query["range"]["lookback"] == "PT3600S"
    assert query["window"]["size"] == 60

    filters = query["filters"]["filterGroups"][0]["filters"]
    assert _filter_value(filters, "i_device_id") == "999"
    assert _filter_value(filters, "ifindex") == "42"

    # rollups/sort survive verbatim.
    assert "p95_in-bit-rate" in payload["query"]["rollups"]
    assert me_nms_request == original
