"""
DutHandler - Handles DUT (Device Under Test) interactions.

Uses the pytest duthost fixture to communicate with a real SONiC device.
Uses SonicDbCli from tests/common/helpers/sonic_db.py for database operations.
"""

import json
import logging
from typing import Dict, Any, List

from tests.common.gcu_utils import apply_patch, generate_tmpfile, delete_tmpfile
from tests.common.helpers.sonic_db import SonicDbCli, SonicDbKeyNotFound
from tests.common.platform.interface_utils import sort_ethernet_intfs

logger = logging.getLogger(__name__)


class DutHandler:
    """
    Handles DUT interactions via the pytest duthost fixture.

    Uses SonicDbCli for CONFIG_DB and STATE_DB operations.

    Args:
        duthost: The pytest duthost fixture.
    """

    def __init__(self, duthost):
        self.duthost = duthost
        self._config_db = SonicDbCli(duthost, 'CONFIG_DB')
        self._state_db = SonicDbCli(duthost, 'STATE_DB')

    # ============================================================
    # SHELL COMMANDS
    # ============================================================

    def run_shell(self, cmd: str, check_rc: bool = True) -> str:
        """
        Execute a shell command on the DUT.

        Args:
            cmd: Shell command to execute
            check_rc: If True, raise exception on non-zero return code

        Returns:
            stdout output

        Raises:
            RuntimeError: If command fails (non-zero rc) and check_rc=True
        """
        logger.debug(f"[DUT] Executing: {cmd}")

        try:
            result = self.duthost.shell(cmd)
        except Exception as e:
            logger.error(f"[DUT] Command execution failed: {cmd}")
            raise RuntimeError(f"Failed to execute command: {cmd}") from e

        rc = result.get('rc', 0)
        stdout = result.get('stdout', '')
        stderr = result.get('stderr', '')

        logger.debug(f"[DUT] rc={rc}, stdout={stdout}")
        if stderr:
            logger.debug(f"[DUT] stderr={stderr}")

        if check_rc and rc != 0:
            logger.error(f"[DUT] Command failed (rc={rc}): {cmd}")
            logger.error(f"[DUT] stderr: {stderr}")
            raise RuntimeError(
                f"Command failed with rc={rc}: {cmd}\n"
                f"stdout: {stdout}\n"
                f"stderr: {stderr}"
            )

        return stdout

    # ============================================================
    # DATABASE OPERATIONS
    # ============================================================

    def get_configdb(self, table: str, key: str) -> Dict[str, Any]:
        """Read an entry from CONFIG_DB."""
        try:
            return self._config_db.hget_all(f'{table}|{key}')
        except SonicDbKeyNotFound:
            return {}

    def get_statedb(self, table: str, key: str) -> Dict[str, Any]:
        """Read an entry from STATE_DB."""
        try:
            return self._state_db.hget_all(f'{table}|{key}')
        except SonicDbKeyNotFound:
            return {}

    def del_configdb(self, table: str, key: str) -> None:
        """Delete an entry from CONFIG_DB."""
        cmd = self._config_db._cli_prefix() + f"DEL '{table}|{key}'"
        self._config_db._run_and_check(cmd)

    def exists_configdb(self, table: str, key: str) -> bool:
        """Check if an entry exists in CONFIG_DB."""
        keys = self._config_db.get_keys(f'{table}|{key}', raise_error_when_not_found=False)
        return len(keys) > 0

    def exists_statedb(self, table: str, key: str) -> bool:
        """Check if an entry exists in STATE_DB."""
        keys = self._state_db.get_keys(f'{table}|{key}', raise_error_when_not_found=False)
        return len(keys) > 0

    # ============================================================
    # INTERFACE OPERATIONS
    # ============================================================

    def get_port_status(self, port: str) -> str:
        """Get the operational status of a port.

        Uses duthost.get_interfaces_status() from tests/common/devices/sonic.py.
        """
        interfaces = self.duthost.get_interfaces_status()
        if port in interfaces:
            return interfaces[port].get('oper', 'down')
        return 'down'

    def shutdown_port(self, port: str) -> None:
        """Shutdown (admin down) a port.

        Uses duthost.shutdown() from tests/common/devices/sonic.py.
        """
        self.duthost.shutdown(port)

    def startup_port(self, port: str) -> None:
        """Startup (admin up) a port.

        Uses duthost.no_shutdown() from tests/common/devices/sonic.py.
        """
        self.duthost.no_shutdown(port)

    def get_ethernet_interfaces(self) -> List[str]:
        """Get list of available (operationally up) Ethernet interfaces.

        Uses SonicDbCli for STATE_DB operations.
        """
        # Get all Ethernet port keys from STATE_DB
        port_keys = self._state_db.get_keys('PORT_TABLE|Ethernet*', raise_error_when_not_found=False)

        up_ports = []
        for key in port_keys:
            # Check the netdev_oper_status field for each port
            try:
                status = self._state_db.hget_key_value(key, 'netdev_oper_status')
                if status == "up":
                    # Extract 'EthernetX' from 'PORT_TABLE|EthernetX'
                    port_name = key.split('|')[1]
                    up_ports.append(port_name)
            except SonicDbKeyNotFound:
                # Skip ports without netdev_oper_status field
                continue

        return sort_ethernet_intfs(up_ports)

    # ============================================================
    # MONITOR LINK OPERATIONS
    # ============================================================

    def show_monitor_link(self, group_name: str) -> str:
        """Get the output of 'show monitor-link <group_name>' CLI command."""
        return self.run_shell(f"show monitor-link {group_name}")

    # ============================================================
    # CONFIG OPERATIONS
    # ============================================================

    def load_config(self, config: Dict[str, Any]) -> None:
        """Load a configuration into the DUT."""
        # Write config to a temp file and load it
        config_json = json.dumps(config)
        self.run_shell(f"echo '{config_json}' > /tmp/config_load.json")
        self.run_shell("config load /tmp/config_load.json -y")
        self.run_shell("rm /tmp/config_load.json")

    def patch_config(self, patch: List[Dict[str, Any]]) -> None:
        """Apply a JSON patch to the running configuration.

        Uses apply_patch() from tests/common/gcu_utils.py.
        """
        tmpfile = generate_tmpfile(self.duthost)
        try:
            output = apply_patch(self.duthost, patch, tmpfile)
            if output['rc'] != 0:
                raise RuntimeError(
                    f"Failed to apply patch: {output.get('stderr', output.get('stdout', ''))}"
                )
        finally:
            delete_tmpfile(self.duthost, tmpfile)
