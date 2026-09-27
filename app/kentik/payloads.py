"""Templates the captured payloads verbatim and substitutes only the fields CLAUDE.md
names. Filters are matched by `filterField`, not list position — the DE templates list
input_port before i_device_id while the NMS template lists i_device_id before ifindex,
so positional substitution would silently swap values on one of the three payloads."""

import copy
import json
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"


def load_template(name: str) -> dict:
    return json.loads((TEMPLATES_DIR / name).read_text())


def _set_filter_values(filter_groups: list[dict], values_by_field: dict[str, str]) -> None:
    for group in filter_groups:
        for f in group.get("filters", []):
            field = f.get("filterField")
            if field in values_by_field:
                f["filterValue"] = values_by_field[field]


def build_de_payload(
    template: dict, *, device_id: int, ifindex: int, lookback_seconds: int, bucket_seconds: int,
) -> dict:
    payload = copy.deepcopy(template)
    query = payload["queries"][0]["query"]
    query["lookback_seconds"] = lookback_seconds
    query["minsPolling"] = bucket_seconds // 60
    _set_filter_values(
        query["filters"]["filterGroups"],
        {"i_device_id": str(device_id), "input_port": str(ifindex)},
    )
    return payload


def build_nms_payload(
    template: dict, *, device_id: int, ifindex: int, lookback_seconds: int, bucket_seconds: int,
) -> dict:
    payload = copy.deepcopy(template)
    query = payload["query"]
    query["range"]["lookback"] = f"PT{lookback_seconds}S"
    query["window"]["size"] = bucket_seconds
    _set_filter_values(
        query["filters"]["filterGroups"],
        {"i_device_id": str(device_id), "ifindex": str(ifindex)},
    )
    return payload
