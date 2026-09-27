class KentikError(Exception):
    """Base for all errors raised while calling or parsing Kentik's APIs."""


class CredentialsRejected(KentikError):
    """Kentik returned 401 for a required call (flow or DE-SNMP)."""


class NoDataError(KentikError):
    """A required call returned zero rows for this device/interface/timespan."""


class ShapeError(KentikError):
    """A required call's response didn't match the shape the validation gates assume
    (e.g. more than one data row, or timeSeries didn't have exactly one key)."""


class UpstreamTimeout(KentikError):
    """A required call timed out after exhausting its retry budget."""


class UpstreamError(KentikError):
    """A required call failed with a non-401 error after exhausting its retry budget."""


class NoComparisonDataError(KentikError):
    """Flow succeeded but both DE-SNMP and NMS failed, leaving nothing to compare it
    against."""
