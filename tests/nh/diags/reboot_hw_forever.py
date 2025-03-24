# Copyright 2025 Nexthop Systems Inc. All rights reserved.
#
# This software is proprietary and confidential. Unauthorized use, distribution,
# or modification is strictly prohibited.

import pytest
from tests.common.reboot import REBOOT_TYPE_COLD
from tests.platform_tests.test_reboot import reboot_and_check
import logging

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]


def test_reboot_hw_forever(duthost, localhost, conn_graph_facts):
    """
    Reboot the dut forever using the on-dut cold reboot methods.
    """
    check_all_xcvrs = {duthost.hostname: []}
    num_failures = 0
    for count in range(15):
        logger.warn(f"Rebooting dut for {count + 1}'th time. {num_failures} failures")
        try:
            reboot_and_check(localhost, duthost, conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {}),
                             check_all_xcvrs, reboot_type=REBOOT_TYPE_COLD)
        except Exception as e:
            logger.error(f"Reboot failed: {e}")
            num_failures += 1
