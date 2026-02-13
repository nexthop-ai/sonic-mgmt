"""
Nexthop AI platform specific reboot-cause test.

This will be refactored to a vendor agnostic test after
https://github.com/sonic-net/sonic-buildimage/issues/25420 is resolved.
"""
import pytest
import logging
import re
from tests.common.reboot import REBOOT_TYPE_COLD
from tests.platform_tests.test_reboot import reboot_and_check

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]


def test_cold_reboot_triggers_powercycle(duthost, localhost, conn_graph_facts):
    """
    @summary: Test that a cold reboot triggers a full system power cycle.
    """
    logger.warn("Testing test_cold_reboot_triggers_powercycle")

    try:
        interfaces = conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {})
        reboot_and_check(
                localhost,
                duthost,
                interfaces,
                xcvr_skip_list={duthost.hostname: []},
                reboot_type=REBOOT_TYPE_COLD)
    except Exception as e:
        pytest.fail(f"Reboot failed: {e}")

    # sudo nh_reboot_cause covers nh_reboot_cause
    output = duthost.shell("sudo nh_reboot_cause")
    res = output['stdout']

    desc_match = re.search(r'description:\s*(.+)', res)
    if not desc_match:
        pytest.fail(f"Failed to extract desc from nh_reboot_cause:\n{res}")

    description = desc_match.group(1).strip() if desc_match else None

    assert "power cycle" in description, f"Did not find 'power cycle' in description: {description}"
