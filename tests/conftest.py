import json
from pathlib import Path

import pytest

from app.kentik.payloads import load_template

# Synthetic responses — see fixtures/generate.py for what each one plants.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


# Request "fixtures" are the payload templates themselves — the app sends them verbatim
# apart from the substituted fields, so there's nothing separate to capture.
@pytest.fixture
def de_flow_request():
    return load_template("de_flow.json")


@pytest.fixture
def me_nms_request():
    return load_template("nms.json")


@pytest.fixture
def de_flow_response():
    return _load("de_flow_response.json")


@pytest.fixture
def de_snmp_response():
    return _load("de_snmp_response.json")


@pytest.fixture
def me_nms_response():
    return _load("me_nms_response.json")


@pytest.fixture
def de_fps_response():
    return _load("de_fps_response.json")


@pytest.fixture
def de_sampling_rate_response():
    return _load("de_sampling_rate_response.json")
