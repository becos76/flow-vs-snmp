"""Phase-1 constants. TIMESPAN is fixed to "1day" — the other TIMESPANS rows exist so
adding a selector later is a lookup change, not a new mapping."""

TIMESPANS = {
    "1hr": {"lookback_seconds": 3600, "bucket_seconds": 60},
    "1day": {"lookback_seconds": 86400, "bucket_seconds": 300},
    "3days": {"lookback_seconds": 259200, "bucket_seconds": 900},
    "1week": {"lookback_seconds": 604800, "bucket_seconds": 3600},
}

TIMESPAN = "1day"

CLUSTER_BASE_URLS = {
    "US": "https://api.kentik.com",
    "EU": "https://api.kentik.eu",
}

DE_ENDPOINT = "/api/v5/query/topxdata"
DE_URL_ENDPOINT = "/api/v5/query/url"
NMS_ENDPOINT = "/api/next/v5/query/nms"

# retries = additional attempts after the first, so 2 retries == 3 attempts total.
RETRY_BUDGETS = {
    "required": {"retries": 2, "timeout_s": 30, "backoff_base_s": 1.0},
    "optional": {"retries": 1, "timeout_s": 15, "backoff_base_s": 1.0},
}

# UI rendering constant only — analysis.py has no notion of "suspect".
SUSPECT_THRESHOLD_PCT = 20.0
