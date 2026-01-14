"""
ConfigDbVerifier - Verifies CONFIG_DB entries against expected configuration.

Fetches CONFIG_DB entry via DutHandler and compares against group_config.

Schema Definitions:
-------------------

1. GROUP_CONFIG SCHEMA (Internal tracking, from monitor_link_groups):
   The source of truth for what was configured.
   {
       'group-1': {
           'uplinks': ['Ethernet0', 'Ethernet4'],    # List of interface names
           'downlinks': ['Ethernet8'],               # List of interface names
           'startup-delay': '5',                     # As string
           'min-uplinks': '1'                        # As string
       }
   }

2. CONFIG_DB RAW OUTPUT SCHEMA (from sonic-db-cli HGETALL):
   Raw output from CONFIG_DB.
   {
       'uplinks': str,            # Comma-separated (e.g., 'Ethernet0,Ethernet4') or '@' suffix
       'downlinks': str,          # Comma-separated (e.g., 'Ethernet8')
       'startup-delay': str,      # e.g., '5'
       'min-uplinks': str         # e.g., '1'
   }

3. COMMON/SANITIZED SCHEMA (Used for matching):
   Normalized format for comparison.
   {
       'uplinks': List[str],      # Sorted list of interface names
       'downlinks': List[str],    # Sorted list of interface names
       'startup-delay': str,      # As string
       'min-uplinks': str         # As string
   }
"""

import logging
from typing import Dict, Any

from ..monitor_link_groups import MonitorLinkGroups
from .db import DbVerifier

logger = logging.getLogger(__name__)


class ConfigDbVerifier(DbVerifier):
    """
    Verifies CONFIG_DB entries against group_handler.

    This class:
    1. Uses group_handler as expected values
    2. Fetches CONFIG_DB entry via DutHandler
    3. Sanitizes both to common schema
    4. Compares and reports mismatches

    Args:
        duthandler: DutHandler instance (real or mock)
        group_handler: MonitorLinkGroups instance for dynamic group config access
    """

    DB_NAME = "CONFIG_DB"
    TABLE_NAME = "MONITOR_LINK_GROUP"
    LIST_KEYS = ['uplinks', 'downlinks']

    def __init__(self, duthandler, group_handler: MonitorLinkGroups):
        super().__init__(duthandler)
        self.group_handler = group_handler

    def _get_expected(self, group_name: str, expected_params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Get expected values from group_handler."""
        return self.group_handler.get(group_name)

    def verify_not_exists(self, group_name: str) -> None:
        """Verify CONFIG_DB entry does NOT exist."""
        logger.info(f"[{self.DB_NAME}] Verifying group '{group_name}' does NOT exist")
        exists = self.duthandler.exists_configdb(self.TABLE_NAME, group_name)
        if exists:
            logger.error(f"[{self.DB_NAME}] Verification FAILED - '{group_name}' still exists")
            assert False, f"{self.DB_NAME} entry for '{group_name}' still exists"
        logger.info(f"[{self.DB_NAME}] Verification PASSED - '{group_name}' does not exist")

    def verify(self, group_name: str) -> bool:
        """
        Verify CONFIG_DB entry matches group_handler config.

        Args:
            group_name: Monitor link group name

        Returns:
            True if verification passes, False otherwise.
        """
        logger.info(f"[{self.DB_NAME}] Verifying group '{group_name}'")

        # Check entry exists
        if not self.duthandler.exists_configdb(self.TABLE_NAME, group_name):
            logger.debug(f"[{self.DB_NAME}] Entry for '{group_name}' does not exist")
            return False

        # Check group_handler has this group
        if not self.group_handler.exists(group_name):
            logger.debug(f"Group '{group_name}' not found in group_handler")
            return False

        # Get and sanitize DB output
        raw_data = self.duthandler.get_configdb(self.TABLE_NAME, group_name)
        actual = self._sanitize_db_output(raw_data)
        logger.debug(f"[{self.DB_NAME}] Actual data: {actual}")

        # Expected values from group_handler
        expected = self._get_expected(group_name)
        logger.debug(f"[{self.DB_NAME}] Expected data: {expected}")

        # Compare
        mismatches = self._compare(expected, actual)
        if mismatches:
            logger.debug(f"[{self.DB_NAME}] Verification mismatch for '{group_name}': {mismatches}")
            return False

        logger.info(f"[{self.DB_NAME}] Verification PASSED for '{group_name}'")
        return True
