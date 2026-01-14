"""
MonitorLinkVerifier - Verifies 'show monitor-link' CLI output against expected params.

Handles parsing of CLI output and comparison logic.
Expects pre-converted params with interface names (not indices).
"""

import logging
import re
from typing import Dict, Any, List

from ..dut_handler import DutHandler

logger = logging.getLogger(__name__)


class MonitorLinkVerifier:
    """
    Verifies 'show monitor-link' CLI output against expected parameters.

    Expects pre-converted params with interface names (converted by Verifier class).

    Args:
        duthandler: DutHandler instance (real or mock)
    """

    def __init__(self, duthandler: DutHandler):
        self.duthandler = duthandler

    def verify(self, group_name: str, expected_params: Dict[str, Any]) -> bool:
        """
        Verify CLI output matches expected params.

        Args:
            group_name: Monitor link group name
            expected_params: Pre-converted params with interface names:
                {
                    'state': str,              # 'up' or 'down'
                    'uplink_status': {         # interface name -> status
                        'Ethernet0': 'up',
                        'Ethernet4': 'down'
                    },
                    'downlink_status': {       # interface name -> status
                        'Ethernet8': 'up'
                    },
                    'uplinks_up': int,         # Number of uplinks that are UP
                    'uplinks_total': int,      # Total number of uplinks
                    'min_uplinks': int,        # min-uplinks config value
                    'startup_delay': int,      # startup-delay config value
                    'description': str,        # Group description
                    'total_interfaces': int,   # Total interfaces count
                    'num_uplinks': int,        # Number of uplinks
                    'num_downlinks': int       # Number of downlinks
                }

        Raises:
            AssertionError: If verification fails.
        """
        logger.info(f"[CLI] Verifying group '{group_name}'")

        # Get and parse CLI output
        output = self.duthandler.show_monitor_link(group_name)
        parsed = self.parse_cli_output(output)

        assert group_name in parsed, f"Group '{group_name}' not found in CLI output"
        actual = parsed[group_name]
        logger.debug(f"[CLI] Actual data: {actual}")

        # Derive expected dict from pre-converted params
        expected = self._derive_expected(expected_params)
        logger.debug(f"[CLI] Expected data: {expected}")

        # Compare
        mismatches = self._compare_dicts(expected, actual)
        if mismatches:
            logger.debug(f"[CLI] Verification mismatch for '{group_name}': {mismatches}")
            return False

        logger.info(f"[CLI] Verification PASSED for '{group_name}'")
        return True

    def parse_cli_output(self, raw_output: str) -> Dict[str, Any]:
        """
        Parse the 'show monitor-link <group_name>' CLI output into a structured dictionary.

        Args:
            raw_output: Raw CLI output string

        Returns:
            Dictionary with group_name as key containing parsed data.
        """
        group_name = None
        group_data = {
            'description': None,
            'state': None,
            'uplinks_up': None,
            'uplinks_total': None,
            'min_uplinks': None,
            'startup_delay': None,
            'total_interfaces': None,
            'num_uplinks': None,
            'num_downlinks': None,
            'uplinks': {},
            'downlinks': {}
        }

        lines = raw_output.strip().splitlines()
        in_interface_table = False

        for line in lines:
            line = line.strip()

            # Skip empty lines and separator lines
            if not line or line.startswith('===') or line.startswith('---'):
                continue

            # Parse group name
            if line.startswith('Monitor Link Group:'):
                group_name = line.split(':', 1)[1].strip()
                continue

            # Parse key-value pairs
            if ':' in line and not in_interface_table:
                group_data = self._parse_key_value_line(line, group_data)
                continue

            # Detect interface table header
            if line.startswith('Interface') and 'Link Type' in line:
                in_interface_table = True
                continue

            # Parse interface table rows
            if in_interface_table:
                group_data = self._parse_interface_row(line, group_data)

        if group_name is None:
            return {}

        return {group_name: group_data}

    def _parse_key_value_line(self, line: str, group_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a key-value line from CLI output."""
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()

        if key == 'Description':
            group_data['description'] = value
        elif key == 'State':
            group_data['state'] = value.lower()
        elif key == 'Uplinks Up':
            match = re.match(r'(\d+)/(\d+)', value)
            if match:
                group_data['uplinks_up'] = int(match.group(1))
                group_data['uplinks_total'] = int(match.group(2))
        elif key == 'Min-uplinks':
            group_data['min_uplinks'] = int(value)
        elif key == 'Startup-delay':
            match = re.match(r'(\d+)', value)
            if match:
                group_data['startup_delay'] = int(match.group(1))
        elif key == 'Total Interfaces':
            match = re.match(r'(\d+)\s*\((\d+)\s*uplinks?,\s*(\d+)\s*downlinks?\)', value)
            if match:
                group_data['total_interfaces'] = int(match.group(1))
                group_data['num_uplinks'] = int(match.group(2))
                group_data['num_downlinks'] = int(match.group(3))

        return group_data

    def _parse_interface_row(self, line: str, group_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse an interface table row from CLI output."""
        parts = line.split()
        if len(parts) >= 3 and (parts[0].startswith('Ethernet') or
                                parts[0].startswith('PortChannel')):
            interface_name = parts[0]
            link_type = parts[1]
            interface_data = {
                'status': parts[2].lower() if len(parts) > 2 else None,
                'reason': parts[3] if len(parts) > 3 else None
            }

            if link_type == 'uplink':
                group_data['uplinks'][interface_name] = interface_data
            else:
                group_data['downlinks'][interface_name] = interface_data

        return group_data

    def _derive_expected(self, expected_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Derive full expected dict from pre-converted params.

        Expects interface names already converted (not indices):
            {
                'state': 'up',
                'uplink_status': {'Ethernet0': 'up', 'Ethernet4': 'down'},
                'downlink_status': {'Ethernet8': 'up'}
            }

        Converts to parser output schema:
            {
                'state': 'up',
                'uplinks': {
                    'Ethernet0': {'status': 'up'},
                    'Ethernet4': {'status': 'down'}
                },
                'downlinks': {
                    'Ethernet8': {'status': 'up'}
                }
            }
        """
        derived = {}

        # Direct copy fields
        for key in ['state', 'uplinks_up', 'uplinks_total', 'min_uplinks',
                    'startup_delay', 'description', 'total_interfaces',
                    'num_uplinks', 'num_downlinks']:
            if key in expected_params:
                derived[key] = expected_params[key]

        # Build uplinks dict from uplink_status (interface name -> status)
        uplink_status = expected_params.get('uplink_status', {})
        if uplink_status:
            derived['uplinks'] = {
                intf: {'status': status} for intf, status in uplink_status.items()
            }

        # Build downlinks dict from downlink_status (interface name -> status)
        downlink_status = expected_params.get('downlink_status', {})
        if downlink_status:
            derived['downlinks'] = {
                intf: {'status': status} for intf, status in downlink_status.items()
            }

        return derived

    def _compare_dicts(self, expected: Dict, actual: Dict, prefix: str = "") -> List[str]:
        """
        Compare expected vs actual dict. Only checks keys present in expected.

        Returns:
            List of mismatch strings.
        """
        mismatches = []

        for key, exp_val in expected.items():
            full_key = f"{prefix}{key}" if prefix else key

            if key not in actual:
                mismatches.append(f"{full_key}: not found in actual")
                continue

            act_val = actual[key]

            # Nested dict comparison
            if isinstance(exp_val, dict) and isinstance(act_val, dict):
                mismatches.extend(self._compare_dicts(exp_val, act_val, f"{full_key}."))
            # Direct comparison
            elif exp_val != act_val:
                mismatches.append(f"{full_key}: expected '{exp_val}', got '{act_val}'")

        return mismatches
