"""
StateDbVerifier - Verifies STATE_DB entries against expected params.

Handles parsing of STATE_DB output and comparison logic.
Expects pre-converted params with interface names (not indices).

Schema Definitions:
-------------------

1. EXPECTED_PARAMS SCHEMA (Interface name-based, pre-converted by Verifier):
   {
       'state': str,              # 'up' or 'down' - group operational state
       'description': str,        # Group description
       'min_uplinks': int,        # Minimum uplinks threshold
       'startup_delay': int,      # Startup delay in seconds
       'uplink_status': {         # Per-uplink status by interface name
           'Ethernet0': 'up',
           'Ethernet4': 'down'
       }
   }

2. STATE_DB RAW OUTPUT SCHEMA (from sonic-db-cli HGETALL):
   Raw output from STATE_DB before parsing.
   {
       'state': str,              # 'up' or 'down'
       'description': str,        # Group description
       'uplinks': str,            # Comma-separated interface names (e.g., 'Ethernet0,Ethernet4')
       'uplinks_at_down': str,    # Uplinks that are down (comma-separated)
       'downlinks': str,          # Comma-separated interface names
       'link_up_threshold': str,  # min_uplinks as string (e.g., '1')
       'link_up_delay': str       # startup_delay as string (e.g., '5')
   }

3. COMMON/SANITIZED SCHEMA (Used for matching):
   Normalized format for comparison - lists are sorted, keys cleaned.
   {
       'state': str,              # 'up' or 'down'
       'description': str,        # Group description
       'uplinks': List[str],      # Sorted list of interface names
       'uplinks_at_down': List[str],  # Sorted list of down uplinks
       'downlinks': List[str],    # Sorted list of interface names
       'link_up_threshold': str,  # min_uplinks as string
       'link_up_delay': str       # startup_delay as string
   }
"""

import logging
from typing import Dict, Any

from .db import DbVerifier

logger = logging.getLogger(__name__)


class StateDbVerifier(DbVerifier):
    """
    Verifies STATE_DB entries against expected parameters.

    This class:
    1. Accepts simple test params (using indices for uplinks/downlinks)
    2. Derives expected values in common schema
    3. Uses DutHandler to get STATE_DB entry
    4. Parses/sanitizes STATE_DB output to common schema
    5. Compares and reports mismatches

    Args:
        duthandler: DutHandler instance (real or mock)
    """

    DB_NAME = "STATE_DB"
    TABLE_NAME = "MONITOR_LINK_GROUP_STATE"
    LIST_KEYS = ['uplinks', 'downlinks', 'uplinks_at_down']

    def _get_expected(
        self, group_name: str, expected_params: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """Derive expected values from expected_params."""
        del group_name  # Unused, but required by base class interface
        return self._derive_expected(expected_params or {})

    def verify_not_exists(self, group_name: str) -> None:
        """Verify STATE_DB entry does NOT exist."""
        logger.info(f"[{self.DB_NAME}] Verifying group '{group_name}' does NOT exist")
        exists = self.duthandler.exists_statedb(self.TABLE_NAME, group_name)
        if exists:
            logger.error(f"[{self.DB_NAME}] Verification FAILED - '{group_name}' still exists")
            assert False, f"{self.DB_NAME} entry for '{group_name}' still exists"
        logger.info(f"[{self.DB_NAME}] Verification PASSED - '{group_name}' does not exist")

    def verify(self, group_name: str, expected_params: Dict[str, Any]) -> bool:
        """
        Verify STATE_DB entry matches expected params.

        Args:
            group_name: Monitor link group name
            expected_params: Test params (see EXPECTED_PARAMS SCHEMA in module docstring)

        Raises:
            AssertionError: If verification fails.
        """
        logger.info(f"[{self.DB_NAME}] Verifying group '{group_name}'")

        # Check entry exists
        exists = self.duthandler.exists_statedb(self.TABLE_NAME, group_name)
        assert exists, f"{self.DB_NAME} entry for '{group_name}' does not exist"

        # Get and sanitize DB output
        raw_data = self.duthandler.get_statedb(self.TABLE_NAME, group_name)
        actual = self._sanitize_db_output(raw_data)
        logger.debug(f"[{self.DB_NAME}] Actual data: {actual}")

        # Derive expected in common schema
        expected = self._get_expected(group_name, expected_params)
        logger.debug(f"[{self.DB_NAME}] Expected data: {expected}")

        if not expected:
            logger.info(f"[{self.DB_NAME}] Verification PASSED for '{group_name}' (no params to check)")
            return True  # Nothing to verify

        # Compare
        mismatches = self._compare(expected, actual)
        if mismatches:
            logger.debug(f"[{self.DB_NAME}] Verification mismatch for '{group_name}': {mismatches}")
            return False

        logger.info(f"[{self.DB_NAME}] Verification PASSED for '{group_name}'")
        return True

    def _derive_expected(self, expected_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Derive expected dict in common schema from pre-converted params.

        Expects interface names already converted (not indices):
            {
                'state': 'up',
                'min_uplinks': 1,
                'uplink_status': {'Ethernet0': 'up', 'Ethernet4': 'down'},
                'downlink_status': {'Ethernet8': 'up'}
            }

        Converts to common schema:
            {
                'state': 'up',
                'link_up_threshold': '1',
                'uplinks': ['Ethernet0', 'Ethernet4'],
                'uplinks_at_down': ['Ethernet4'],
                'downlinks': ['Ethernet8']
            }
        """
        derived = {}

        # Direct mappings
        if 'state' in expected_params:
            derived['state'] = expected_params['state']

        if 'description' in expected_params:
            derived['description'] = expected_params['description']

        # Map min_uplinks -> link_up_threshold (as string)
        if 'min_uplinks' in expected_params:
            derived['link_up_threshold'] = str(expected_params['min_uplinks'])

        # Map startup_delay -> link_up_delay (as string)
        if 'startup_delay' in expected_params:
            derived['link_up_delay'] = str(expected_params['startup_delay'])

        # Derive uplinks and uplinks_at_down from uplink_status
        if 'uplink_status' in expected_params:
            uplink_status = expected_params['uplink_status']
            # uplinks = all interface names from uplink_status
            derived['uplinks'] = sorted(uplink_status.keys())
            # uplinks_at_down = interfaces with 'down' status
            down_uplinks = [intf for intf, status in uplink_status.items() if status == 'down']
            derived['uplinks_at_down'] = sorted(down_uplinks)

        # Derive downlinks from downlink_status
        if 'downlink_status' in expected_params:
            downlink_status = expected_params['downlink_status']
            derived['downlinks'] = sorted(downlink_status.keys())

        return derived
