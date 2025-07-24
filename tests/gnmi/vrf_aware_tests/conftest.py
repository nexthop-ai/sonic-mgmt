import pytest

from tests.common.utilities import DEFAULT_VRF_NAME, MGMT_VRF_NAME


VRF_SCENARIOS = [
    {"name": "default_1", "vrf": None, "description": "Default (no VRF)"},
    {"name": "default_2", "vrf": DEFAULT_VRF_NAME, "description": "Default (explicit 'default')"},
    {"name": "mgmt", "vrf": MGMT_VRF_NAME, "description": "Management VRF"},
    {"name": "custom", "vrf": "Vrf-FOO", "description": "Custom VRF (Vrf-FOO)"}
]


@pytest.fixture(scope="module", params=VRF_SCENARIOS, ids=lambda scenario: f"vrf_{scenario['name']}")
def vrf_config(request):
    return request.param
