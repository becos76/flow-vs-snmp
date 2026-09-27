"""Calls Kentik's Query API with the retry/timeout budgets from CLAUDE.md's "Retries,
timeouts, and error handling": retryable failures (timeout, connection error, 429, 5xx)
back off exponentially with jitter; 401 and other 4xx fail immediately, since retrying
a rejected credential or a malformed payload just wastes the budget on something that
can't change. Required calls (flow, DE-SNMP) get a bigger budget than the optional NMS
control call, matching the partial-failure policy."""

import asyncio
import random

import httpx

from app.config import CLUSTER_BASE_URLS, DE_ENDPOINT, DE_URL_ENDPOINT, NMS_ENDPOINT, RETRY_BUDGETS
from app.kentik.exceptions import (
    CredentialsRejected,
    NoComparisonDataError,
    UpstreamError,
    UpstreamTimeout,
)
from app.kentik.parse import parse_de_sample_rate_breakdown, parse_de_series, parse_nms_series
from app.kentik.payloads import build_de_payload, build_nms_payload, load_template

RETRYABLE_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.TransportError)

# Kentik's per-customer hard cap is 4 concurrent Query API requests (see CLAUDE.md's
# "Retries, timeouts, and error handling"). fetch_all now schedules 9 calls per analyze
# click (flow, DE-SNMP, NMS, FPS, sample-rate breakdown, and a portal-link lookup for
# each of the 4 DE queries), so a semaphore keeps at most 4 of them in flight at once.
# Built fresh inside fetch_all (not at module level) since app.py drives this with a new
# asyncio.run() per click — a module-level asyncio primitive built on Python 3.9 binds to
# whatever loop happens to be current at import time, which is not the loop asyncio.run()
# later creates.


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict, payload: dict, budget: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    attempts = budget["retries"] + 1
    backoff = budget["backoff_base_s"]
    last_error = None

    for attempt in range(attempts):
        is_last_attempt = attempt == attempts - 1
        try:
            async with semaphore:
                response = await client.post(url, headers=headers, json=payload, timeout=budget["timeout_s"])
        except RETRYABLE_TRANSPORT_ERRORS as exc:
            last_error = exc
            if is_last_attempt:
                raise UpstreamTimeout(f"{url}: {exc}") from exc
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff *= 2
            continue

        if response.status_code == 401:
            raise CredentialsRejected("credentials rejected")

        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"status {response.status_code}"
            if is_last_attempt:
                raise UpstreamError(f"{url}: {last_error}")
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff *= 2
            continue

        if response.status_code >= 400:
            raise UpstreamError(f"{url}: status {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"{url}: response was not valid JSON") from exc

    raise UpstreamError(f"{url}: {last_error}")


def _build_de_payload(template_name, *, device_id, ifindex, lookback_seconds, bucket_seconds):
    template = load_template(template_name)
    return build_de_payload(
        template, device_id=device_id, ifindex=ifindex,
        lookback_seconds=lookback_seconds, bucket_seconds=bucket_seconds,
    )


async def _fetch_de_series(
    client, headers, base_url, payload, *, budget, semaphore,
    capture=None, capture_key=None, parser=parse_de_series,
):
    if capture is not None:
        capture[f"{capture_key}_request"] = payload
    body = await _post_with_retry(client, base_url + DE_ENDPOINT, headers, payload, budget, semaphore)
    if capture is not None:
        capture[f"{capture_key}_response"] = body
    return parser(body)


async def _fetch_de_portal_url(
    client, headers, base_url, payload, *, budget, semaphore, capture=None, capture_key=None,
):
    """Kentik's /query/url endpoint takes the same payload as topxdata and returns a
    plain JSON-encoded string (not an object) — a link to view the identical query in
    the Kentik portal. Purely a UI convenience for the Request/Response popovers, so
    failures here are silently dropped by the caller rather than surfaced as a warning
    the way a real comparison-source failure would be."""
    body = await _post_with_retry(client, base_url + DE_URL_ENDPOINT, headers, payload, budget, semaphore)
    if capture is not None:
        capture[f"{capture_key}_portal_url"] = body
    return body


async def _fetch_nms_series(
    client, headers, base_url, *, device_id, ifindex, lookback_seconds, bucket_seconds,
    budget, semaphore, capture=None, capture_key=None,
):
    template = load_template("nms.json")
    payload = build_nms_payload(
        template, device_id=device_id, ifindex=ifindex,
        lookback_seconds=lookback_seconds, bucket_seconds=bucket_seconds,
    )
    if capture is not None:
        capture[f"{capture_key}_request"] = payload
    body = await _post_with_retry(client, base_url + NMS_ENDPOINT, headers, payload, budget, semaphore)
    if capture is not None:
        capture[f"{capture_key}_response"] = body
    return parse_nms_series(body)


async def fetch_all(
    *, email: str, api_token: str, cluster: str, device_id: int, ifindex: int,
    lookback_seconds: int, bucket_seconds: int, capture: dict | None = None,
):
    """Fires flow, DE-SNMP, NMS, FPS, the sample-rate breakdown, and a Kentik
    portal-link lookup for each of the four DE (topxdata) queries — 9 calls total,
    capped at 4-in-flight via a semaphore (Kentik's per-customer hard limit — see
    CLAUDE.md's "Retries, timeouts, and error handling"). Only flow is required — its
    failure propagates. DE-SNMP and NMS each degrade independently into (None, warning) on
    failure; if both of them fail, there's nothing left to compare flow against, so that
    combination raises NoComparisonDataError. FPS and the sample-rate breakdown are a
    Phase-2 diagnostic on top of that core comparison, so each degrades independently the
    same way DE-SNMP/NMS do, but neither failure escalates to NoComparisonDataError even
    if both fail. The 4 portal-link lookups are a pure UI convenience (see
    _fetch_de_portal_url) — their failures aren't warned about at all, just absent from
    `capture`. See CLAUDE.md's "Partial failure policy".
    Returns (flow, snmp_de_or_None, snmp_nms_or_None, fps_or_None,
    sample_rate_breakdown_or_None, warnings).

    If `capture` is passed, each source's request payload (always) and response body (only
    on success) are stashed into it under f"{name}_request"/f"{name}_response" — the caller's
    dict is mutated in place, so whatever was captured survives even when this function goes
    on to raise (e.g. a failed required flow call still leaves any already-captured
    comparison-source payloads/responses in `capture`). The 4 DE sources additionally get
    f"{name}_portal_url" on success."""
    base_url = CLUSTER_BASE_URLS[cluster]
    headers = {"X-CH-Auth-Email": email, "X-CH-Auth-API-Token": api_token}
    required = RETRY_BUDGETS["required"]
    optional = RETRY_BUDGETS["optional"]
    semaphore = asyncio.Semaphore(4)

    payload_kwargs = dict(
        device_id=device_id, ifindex=ifindex,
        lookback_seconds=lookback_seconds, bucket_seconds=bucket_seconds,
    )
    flow_payload = _build_de_payload("de_flow.json", **payload_kwargs)
    snmp_payload = _build_de_payload("de_snmp.json", **payload_kwargs)
    fps_payload = _build_de_payload("de_fps.json", **payload_kwargs)
    rate_payload = _build_de_payload("de_sample_rate.json", **payload_kwargs)

    async with httpx.AsyncClient() as client:
        (
            flow_result, snmp_result, nms_result, fps_result, rate_result,
            *_portal_url_results,
        ) = await asyncio.gather(
            _fetch_de_series(
                client, headers, base_url, flow_payload, budget=required,
                semaphore=semaphore, capture=capture, capture_key="flow",
            ),
            _fetch_de_series(
                client, headers, base_url, snmp_payload, budget=required,
                semaphore=semaphore, capture=capture, capture_key="snmp",
            ),
            _fetch_nms_series(
                client, headers, base_url,
                device_id=device_id, ifindex=ifindex,
                lookback_seconds=lookback_seconds, bucket_seconds=bucket_seconds, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="nms",
            ),
            _fetch_de_series(
                client, headers, base_url, fps_payload, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="fps",
            ),
            _fetch_de_series(
                client, headers, base_url, rate_payload, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="rate",
                parser=parse_de_sample_rate_breakdown,
            ),
            # Portal-link lookups (see _fetch_de_portal_url) — a UI convenience stashed
            # straight into `capture`, not part of this function's return value, so their
            # results are only unpacked here to keep gather() from raising on them.
            _fetch_de_portal_url(
                client, headers, base_url, flow_payload, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="flow",
            ),
            _fetch_de_portal_url(
                client, headers, base_url, snmp_payload, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="snmp",
            ),
            _fetch_de_portal_url(
                client, headers, base_url, fps_payload, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="fps",
            ),
            _fetch_de_portal_url(
                client, headers, base_url, rate_payload, budget=optional,
                semaphore=semaphore, capture=capture, capture_key="rate",
            ),
            return_exceptions=True,
        )

    if isinstance(flow_result, Exception):
        raise flow_result

    warnings = []
    if isinstance(snmp_result, Exception) and isinstance(nms_result, Exception):
        raise NoComparisonDataError(
            f"snmp_de: {snmp_result}; snmp_nms: {nms_result}"
        )
    if isinstance(snmp_result, Exception):
        warnings.append(f"SNMP (DE) control unavailable: {snmp_result}")
        snmp_result = None
    if isinstance(nms_result, Exception):
        warnings.append(f"NMS control unavailable: {nms_result}")
        nms_result = None
    if isinstance(fps_result, Exception):
        warnings.append(f"FPS unavailable: {fps_result}")
        fps_result = None
    if isinstance(rate_result, Exception):
        warnings.append(f"Sample rate breakdown unavailable: {rate_result}")
        rate_result = None

    return flow_result, snmp_result, nms_result, fps_result, rate_result, warnings
