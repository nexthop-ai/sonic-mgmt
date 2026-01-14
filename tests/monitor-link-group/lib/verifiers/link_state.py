"""
LinkStateVerifier - Verifies link operational states against expected params.

Fetches port states via DutHandler and compares against expected values.

Schema Definitions:
-------------------

1. EXPECTED_PARAMS SCHEMA (Interface name-based, pre-converted by Verifier):
   {
       'uplink_status': {         # Per-uplink status by interface name
           'Ethernet0': 'up',
           'Ethernet4': 'down'
       },
       'downlink_status': {       # Per-downlink status by interface name
           'Ethernet8': 'up'
       }
   }

2. ACTUAL SCHEMA (From DUT):
   Port states fetched from DUT via get_port_status().
   Same format as EXPECTED_PARAMS.
"""

import logging
from typing import Dict, Any, List

from ..dut_handler import DutHandler

logger = logging.getLogger(__name__)


class LinkStateVerifier:
    """
    Verifies link operational states against expected parameters.

    Expects pre-converted params with interface names (not indices).

    Args:
        duthandler: DutHandler instance (real or mock)
    """

    def __init__(self, duthandler: DutHandler):
        self.duthandler = duthandler

    def verify(self, group_name: str, expected_params: Dict[str, Any]) -> bool:
        """
        Verify link states match expected params.

        Args:
            group_name: Monitor link group name (unused, kept for API consistency)
            expected_params: Pre-converted params with interface names:
                {
                    'uplink_status': {'Ethernet0': 'up', 'Ethernet4': 'down'},
                    'downlink_status': {'Ethernet8': 'up'}
                }

        Raises:
            AssertionError: If verification fails.
        """
        logger.info(f"[LINK_STATE] Verifying group '{group_name}'")

        uplink_status = expected_params.get('uplink_status', {})
        downlink_status = expected_params.get('downlink_status', {})

        if not uplink_status and not downlink_status:
            logger.info(f"[LINK_STATE] Verification PASSED for '{group_name}' (no params to check)")
            return True  # Nothing to verify

        # Build expected structure
        expected = {
            'uplinks': {intf: status.lower() for intf, status in uplink_status.items()},
            'downlinks': {intf: status.lower() for intf, status in downlink_status.items()}
        }
        logger.debug(f"[LINK_STATE] Expected data: {expected}")

        # Fetch actual states from DUT
        actual = self._fetch_actual_states(expected)
        logger.debug(f"[LINK_STATE] Actual data: {actual}")

        # Compare
        mismatches = self._compare(expected, actual)
        if mismatches:
            logger.debug(f"[LINK_STATE] Verification mismatch for '{group_name}': {mismatches}")
            return False

        logger.info(f"[LINK_STATE] Verification PASSED for '{group_name}'")
        return True

    def _fetch_actual_states(self, expected: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch actual port states from DUT for interfaces in expected.

        Only fetches states for interfaces that are being verified.
        """
        actual: Dict[str, Any] = {'uplinks': {}, 'downlinks': {}}

        for intf in expected.get('uplinks', {}):
            actual['uplinks'][intf] = self.duthandler.get_port_status(intf).lower()

        for intf in expected.get('downlinks', {}):
            actual['downlinks'][intf] = self.duthandler.get_port_status(intf).lower()

        return actual

    def _compare(
        self, expected: Dict[str, Any], actual: Dict[str, Any]
    ) -> List[str]:
        """
        Compare expected vs actual link states.

        Returns:
            List of mismatch strings.
        """
        mismatches = []

        # Compare uplinks
        for intf, exp_status in expected.get('uplinks', {}).items():
            act_status = actual.get('uplinks', {}).get(intf)
            if act_status is None:
                mismatches.append(f"uplink {intf}: not found in actual")
            elif exp_status != act_status:
                mismatches.append(
                    f"uplink {intf}: expected '{exp_status}', got '{act_status}'"
                )

        # Compare downlinks
        for intf, exp_status in expected.get('downlinks', {}).items():
            act_status = actual.get('downlinks', {}).get(intf)
            if act_status is None:
                mismatches.append(f"downlink {intf}: not found in actual")
            elif exp_status != act_status:
                mismatches.append(
                    f"downlink {intf}: expected '{exp_status}', got '{act_status}'"
                )

        return mismatches

    def verify_single_link(
        self, interface: str, expected_state: str, timeout: int = 5
    ) -> None:
        """
        Verify a single link is in the expected operational state.

        Polls until the link reaches the expected state or timeout.

        Args:
            interface: Interface name (e.g., 'Ethernet0')
            expected_state: Expected state ('up' or 'down')
            timeout: Max seconds to wait

        Raises:
            AssertionError: If link doesn't reach expected state within timeout
        """
        from tests.common.utilities import wait_until

        logger.info(f"[LINK_STATE] Verifying '{interface}' is {expected_state}")
        expected_state = expected_state.lower()

        def check_link_state():
            actual_state = self.duthandler.get_port_status(interface).lower()
            return actual_state == expected_state

        assert wait_until(timeout, 0.5, 0, check_link_state), \
            f"Link '{interface}' did not reach state '{expected_state}' within {timeout}s"

        logger.info(f"[LINK_STATE] Verification PASSED - '{interface}' is {expected_state}")
