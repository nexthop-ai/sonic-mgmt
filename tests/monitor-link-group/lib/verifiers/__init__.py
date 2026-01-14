"""
Verifiers package for monitor link testing.

This package contains:
- Verifier: Unified verification class that delegates to specialized verifiers
- DbVerifier: Base class for database verification (CONFIG_DB, STATE_DB)
- MonitorLinkVerifier: CLI output verification
- StateDbVerifier: STATE_DB verification
- ConfigDbVerifier: CONFIG_DB verification
- LinkStateVerifier: Link state verification

Usage:
    from lib.verifiers import Verifier

    verifier = Verifier(duthandler, group_config)
    verifier.verify('group-1', {
        'state': 'up',
        'uplink_status': {0: 'up', 1: 'down'},
        'all_downlinks': 'up'
    })
"""

import logging
from typing import Dict, Any, Optional

from tests.common.utilities import wait_until

from ..dut_handler import DutHandler
from ..monitor_link_groups import MonitorLinkGroups
from .db import DbVerifier
from .cli import MonitorLinkVerifier
from .statedb import StateDbVerifier
from .configdb import ConfigDbVerifier
from .link_state import LinkStateVerifier

logger = logging.getLogger(__name__)

__all__ = [
    'Verifier',
    'DbVerifier',
    'MonitorLinkVerifier',
    'StateDbVerifier',
    'ConfigDbVerifier',
    'LinkStateVerifier',
]


class Verifier:
    """
    Unified verification class that delegates to specialized verifiers.

    This class:
    1. Accepts simple test params (using indices for uplinks/downlinks)
    2. Delegates to appropriate verifiers based on what needs to be verified

    Args:
        duthandler: DutHandler instance (real or mock)
        group_handler: MonitorLinkGroups instance for dynamic group config access

    Expected params schema (test-friendly, index-based):
        {
            # Group state (for CLI and STATE_DB verification)
            'state': str,              # 'up' or 'down'

            # Per-interface status by index
            'uplink_status': {         # index -> 'up' or 'down'
                0: 'up',
                1: 'down'
            },
            'downlink_status': {       # index -> 'up' or 'down'
                0: 'up'
            },

            # Shorthand for all interfaces same status
            'all_uplinks': str,        # 'up' or 'down'
            'all_downlinks': str,      # 'up' or 'down'

            # Additional fields
            'uplinks_up': int,         # Number of uplinks UP (CLI)
            'uplinks_total': int,      # Total uplinks (CLI)
            'min_uplinks': int,        # min-uplinks config
            'startup_delay': int,      # startup-delay config
            'description': str         # Group description
        }
    """

    def __init__(self, duthandler: DutHandler, group_handler: MonitorLinkGroups):
        self.duthandler = duthandler
        self.group_handler = group_handler

        # Initialize specialized verifiers
        self.cli_verifier = MonitorLinkVerifier(duthandler)
        self.statedb_verifier = StateDbVerifier(duthandler)
        self.configdb_verifier = ConfigDbVerifier(duthandler, group_handler)
        self.link_state_verifier = LinkStateVerifier(duthandler)

    def verify(
        self,
        group_name: str,
        expected_params: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Verify monitor link group against expected params.

        Converts index-based params to interface names and delegates to
        all specialized verifiers. Polls each verifier until verification passes.

        Args:
            group_name: Monitor link group name
            expected_params: Test params (see class docstring for schema).
                            If None, uses empty dict.

        Raises:
            AssertionError: If any verification fails.
        """
        if expected_params is None:
            expected_params = {}

        # Convert indices to interface names
        converted = self._convert_indices_to_interfaces(group_name, expected_params)

        # Calculate timeout from startup_delay + buffer
        timeout = int(self.group_handler.get(group_name)['startup-delay']) + 2

        # CONFIG_DB - verify configuration matches
        assert wait_until(timeout, 1, 0, self.configdb_verifier.verify, group_name), \
            f"CONFIG_DB verification timed out for '{group_name}'"

        # STATE_DB - poll until verification passes
        assert wait_until(timeout, 1, 0, self.statedb_verifier.verify, group_name, converted), \
            f"STATE_DB verification timed out for '{group_name}'"

        # CLI - poll until verification passes
        assert wait_until(timeout, 1, 0, self.cli_verifier.verify, group_name, converted), \
            f"CLI verification timed out for '{group_name}'"

        # Link state - poll until verification passes
        assert wait_until(timeout, 1, 0, self.link_state_verifier.verify, group_name, converted), \
            f"Link state verification timed out for '{group_name}'"

    def _convert_indices_to_interfaces(
        self, group_name: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert index-based params to interface name-based params.

        Converts:
            'uplink_status': {0: 'up', 1: 'down'}
        To:
            'uplink_status': {'Ethernet0': 'up', 'Ethernet4': 'down'}

        Also expands 'all_uplinks'/'all_downlinks' to per-interface status.
        """
        group = self.group_handler.get(group_name)
        uplinks = group.get('uplinks', [])
        downlinks = group.get('downlinks', [])

        converted = dict(params)

        # Convert uplink_status indices to interface names
        if 'uplink_status' in converted:
            converted['uplink_status'] = {
                uplinks[idx]: status
                for idx, status in params['uplink_status'].items()
                if idx < len(uplinks)
            }

        # Convert downlink_status indices to interface names
        if 'downlink_status' in converted:
            converted['downlink_status'] = {
                downlinks[idx]: status
                for idx, status in params['downlink_status'].items()
                if idx < len(downlinks)
            }

        # Expand all_uplinks to per-interface status
        if 'all_uplinks' in converted and 'uplink_status' not in converted:
            converted['uplink_status'] = {
                intf: converted['all_uplinks'] for intf in uplinks
            }

        # Expand all_downlinks to per-interface status
        if 'all_downlinks' in converted and 'downlink_status' not in converted:
            converted['downlink_status'] = {
                intf: converted['all_downlinks'] for intf in downlinks
            }

        return converted

    def verify_not_exists(self, group_name: str, timeout: int = 30) -> None:
        """
        Verify that a monitor link group does NOT exist.

        Polls until group is deleted from both CONFIG_DB and STATE_DB.

        Args:
            group_name: Monitor link group name
            timeout: Max seconds to wait.

        Raises:
            AssertionError: If group still exists after timeout.
        """
        logger.info(f"[VERIFIER] Verifying '{group_name}' does NOT exist, timeout={timeout}s")

        def check_not_exists():
            config_exists = self.duthandler.exists_configdb(
                'MONITOR_LINK_GROUP', group_name
            )
            state_exists = self.duthandler.exists_statedb(
                'MONITOR_LINK_GROUP_STATE', group_name
            )
            return not config_exists and not state_exists

        assert wait_until(timeout, 1, 0, check_not_exists), \
            f"Group '{group_name}' still exists after {timeout}s"
        logger.info(f"[VERIFIER] Verification PASSED - '{group_name}' does not exist")

    def verify_link_state(self, interface: str, expected_state: str, timeout: int = 5) -> None:
        """
        Verify that a link is in the expected operational state.

        Polls until the link reaches the expected state.

        Args:
            interface: Interface name (e.g., 'Ethernet0')
            expected_state: Expected state ('up' or 'down')
            timeout: Max seconds to wait

        Raises:
            AssertionError: If link doesn't reach expected state within timeout
        """
        logger.info(f"[VERIFIER] Verifying link '{interface}' is {expected_state}")
        self.link_state_verifier.verify_single_link(interface, expected_state, timeout)
