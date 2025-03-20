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
    This is mostly intended for various chamber testing.
    """
    check_all_xcvrs = {duthost.hostname: []}
    count = 1
    while True:
        logger.warn(f"Rebooting dut for {count}'th time")
        try:
            reboot_and_check(localhost, duthost, conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {}),
                             check_all_xcvrs, reboot_type=REBOOT_TYPE_COLD)
        except Exception as e:
            logger.error(f"Reboot failed: {e}")
        logger.warn("Dut came back")
        count += 1
