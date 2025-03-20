# Copyright 2025 Nexthop Systems Inc. All rights reserved.
#
# This software is proprietary and confidential. Unauthorized use, distribution,
# or modification is strictly prohibited.

import pytest
from tests.common.helpers.assertions import pytest_assert
from tests.common.reboot import REBOOT_TYPE_POWEROFF
from tests.platform_tests.test_reboot import reboot_and_check
from tests.platform_tests.test_power_off_reboot import _power_off_reboot_helper
import logging

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]

def test_reboot_pdu_forever(duthost, localhost, conn_graph_facts, get_pdu_controller):
    """
    Reboot the dut forever using the PDU to cut the power.
    This is mostly intended for various chamber testing.
    """
    pdu = get_pdu_controller(duthost)
    pytest_assert(pdu is not None, "No PDU found")

    dut_outlets = pdu.get_outlet_status()
    reboot_kwargs = {
            "dut": duthost,
            "pdu_ctrl": pdu,
            "all_outlets": dut_outlets,
            "power_on_seq": dut_outlets,
    }

    check_all_xcvrs = {duthost.hostname: []}
    count = 1
    while True:
        logger.warn(f"Rebooting dut for {count}'th time")
        reboot_and_check(localhost, duthost, conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {}),
                         check_all_xcvrs, reboot_type=REBOOT_TYPE_POWEROFF,
                         reboot_helper=_power_off_reboot_helper, reboot_kwargs=reboot_kwargs)
        logger.warn("Dut came back")
        count += 1
