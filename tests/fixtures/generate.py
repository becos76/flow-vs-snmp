"""Regenerates the synthetic Kentik response fixtures in this directory.

The fixtures are fully synthetic — no real device, interface or traffic data. They
reproduce the *shape* of real Kentik responses (key names, nesting, point formats,
rollup fields the parser ignores, scalar-only sample-rate rows) and plant the quirks the
app has to handle, so the tests exercise the same paths real data does:

- 288 points per series at a clean 300 s step, 1 day.
- NMS runs exactly two buckets (600 s) earlier than the DE series, so the three-way outer
  join has 290 timestamps: 286 aligned, 2 NMS-only at the start, 2 DE-only at the end.
- NMS equals DE-SNMP to within 0.05% except three planted outlier buckets where NMS
  reads higher, the worst a 40.0% delta at WORST_OUTLIER_TS.
- Flow under-reports relative to SNMP in most buckets (a sampling-like bias), with a
  handful of buckets where it over-reports.
- The sample-rate breakdown has 50 rows, of which only the top 40 carry a timeSeries.

Deterministic (fixed seed): rerunning produces byte-identical files.

    python tests/fixtures/generate.py
"""

import json
import math
import random
from pathlib import Path

HERE = Path(__file__).parent

BUCKET_MS = 300_000
POINTS = 288
DE_START_MS = 1767225900000  # 2026-01-01T00:05:00Z
NMS_START_MS = DE_START_MS - 2 * BUCKET_MS
WORST_OUTLIER_TS = DE_START_MS + 140 * BUCKET_MS
OUTLIERS = {  # ts -> (snmp_de, snmp_nms); NMS larger in each
    WORST_OUTLIER_TS: (1_200_000.0, 2_000_000.0),  # 40.0%
    DE_START_MS + 60 * BUCKET_MS: (2_000_000.0, 2_600_000.0),  # 23.1%
    DE_START_MS + 220 * BUCKET_MS: (2_100_000.0, 2_700_000.0),  # 22.2%
}
DOMINANT_RATE = "1000"
SECONDARY_RATE = "2000"
SECONDARY_ACTIVE_BUCKETS = range(100, 191)  # 91 buckets


def _r(x: float, nd: int = 2) -> float:
    return round(x, nd)


def _rollups(values: list[float]) -> tuple[float, float, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return _r(sum(values) / len(values), 3), _r(p95, 3), _r(max(values), 3)


def main() -> None:
    rng = random.Random(20260101)
    de_ts = [DE_START_MS + i * BUCKET_MS for i in range(POINTS)]
    nms_ts = [NMS_START_MS + i * BUCKET_MS for i in range(POINTS)]

    # SNMP ground truth over the union of both windows: a diurnal curve plus noise.
    snmp = {}
    for ts in sorted(set(de_ts) | set(nms_ts)):
        hour = ((ts - DE_START_MS) / 3_600_000) % 24
        base = 3_000_000 + 1_000_000 * math.sin((hour - 8) / 24 * 2 * math.pi)
        snmp[ts] = _r(base * rng.uniform(0.95, 1.05))
    for ts, (de_val, _nms_val) in OUTLIERS.items():
        snmp[ts] = de_val

    snmp_de = {ts: snmp[ts] for ts in de_ts}
    snmp_nms = {ts: _r(snmp[ts] * rng.uniform(0.9995, 1.0005)) for ts in nms_ts}
    for ts, (_de_val, nms_val) in OUTLIERS.items():
        snmp_nms[ts] = nms_val

    flow = {}
    for i, ts in enumerate(de_ts):
        ratio = rng.uniform(1.005, 1.04) if i % 14 == 0 else rng.uniform(0.84, 0.995)
        flow[ts] = _r(snmp_de[ts] * ratio)

    def de_response(series: dict, ts_key: str, rollup_prefixes: tuple[str, str, str], suffix: str) -> dict:
        avg, p95, peak = _rollups(list(series.values()))
        return {"results": [{"bucket": "Left +Y Axis", "data": [{
            "name": "Total", "key": "Total",
            f"{rollup_prefixes[0]}{suffix}": avg,
            "timeSeries": {ts_key: {"flow": [[ts, v, 300] for ts, v in series.items()]}},
            f"{rollup_prefixes[1]}{suffix}": p95,
            f"{rollup_prefixes[2]}{suffix}": peak,
        }]}]}

    prefixes = ("avg_", "p95th_", "max_")
    write("de_flow_response.json", de_response(flow, "both_bits_per_sec", prefixes, "bits_per_sec"))
    write("de_snmp_response.json", de_response(
        snmp_de, "int64_00_per_sec", prefixes, "ktappprotocol__snmp__INT64_00",
    ))

    nms_values = list(snmp_nms.values())
    _avg, p95, peak = _rollups(nms_values)
    write("me_nms_response.json", {"data": [{
        "timeseries": [[ts, v] for ts, v in snmp_nms.items()],
        "p95_in-bit-rate": p95, "max_in-bit-rate": peak, "last_in-bit-rate": nms_values[-1],
        "metrics": ["in-bit-rate"],
    }]})

    fps = {ts: _r(80 + 40 * math.sin(i / POINTS * 2 * math.pi) + rng.uniform(-5, 5))
           for i, ts in enumerate(de_ts)}
    write("de_fps_response.json", {"results": [{"bucket": "Left +Y Axis", "data": [{
        "name": "Total", "key": "Total",
        "max_flows_per_sec": max(fps.values()),
        "timeSeries": {"flows_per_sec": {"flow": [[ts, v, 300] for ts, v in fps.items()]}},
        "max_bits_per_sec": max(flow.values()),
        "max_max_sample_rate": int(SECONDARY_RATE),
    }]}]})

    rows = []
    for rank in range(50):
        rate = DOMINANT_RATE if rank == 0 else SECONDARY_RATE if rank == 1 else str(3000 + rank * 17)
        row = {"sample_rate": int(rate), "key": rate}
        if rank < 40:
            if rate == DOMINANT_RATE:
                points = [[ts, fps[ts], 300] for ts in de_ts]
            elif rate == SECONDARY_RATE:
                points = [[ts, _r(rng.uniform(1, 10)) if i in SECONDARY_ACTIVE_BUCKETS else 0, 300]
                          for i, ts in enumerate(de_ts)]
            else:
                active = rng.randrange(POINTS)
                points = [[ts, _r(rng.uniform(0.01, 1)) if i == active else 0, 300]
                          for i, ts in enumerate(de_ts)]
            row["max_flows_per_sec"] = max(p[1] for p in points)
            row["timeSeries"] = {"flows_per_sec": {"flow": points}}
        else:
            row["max_flows_per_sec"] = _r(rng.uniform(0.01, 0.5))
            row["timeSeries"] = {}
        row["max_bits_per_sec"] = _r(rng.uniform(1e5, 1e7), 3)
        row["max_max_sample_rate"] = int(rate)
        rows.append(row)
    write("de_sampling_rate_response.json", {"results": [{"bucket": "Left +Y Axis", "data": rows}]})


def write(name: str, payload: dict) -> None:
    (HERE / name).write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    main()
