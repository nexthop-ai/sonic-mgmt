"""
TestHandler - Main entry point for monitor link test scripts.

Provides a unified API for:
- Group management (create/update/remove)
- Port state management (set ports up/down)
- Verification (verify group state)

Usage:
    handler = TestHandler(duthandler)
    handler.create_group('group-1', num_uplinks=2, num_downlinks=1)
    handler.set_uplink_down('group-1', 0)
    handler.verify('group-1', {'state': 'down'})
    handler.remove_group('group-1')
"""

import logging
from typing import Dict, Any, Optional, List

from tests.common.helpers.intf_mgr import IntfMgr

from .dut_handler import DutHandler
from .monitor_link_groups import MonitorLinkGroups
from .verifiers import Verifier

logger = logging.getLogger(__name__)


class TestHandler:
    """
    Main entry point for monitor link test scripts.

    Consolidates group management, port control, and verification.
    Uses IntfMgr to allocate interfaces automatically.

    Args:
        duthandler: DutHandler instance (real or mock)
        mode: Interface allocation mode - 'ports' or 'mixed' (default: 'ports')
              'ports': All interfaces are individual ports
              'mixed': 1 portchannel + rest as individual ports
    """

    def __init__(self, duthandler: DutHandler, mode: str = 'ports'):
        if mode not in ('ports', 'mixed'):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'ports' or 'mixed'")
        self.duthandler = duthandler
        self.intf_mgr = IntfMgr(duthandler.duthost)
        self.groups = MonitorLinkGroups()
        self.verifier = Verifier(duthandler, self.groups)
        self.mode = mode

    @staticmethod
    def _sort_portchannels_first(interfaces: List[str]) -> List[str]:
        """Sort interfaces with PortChannels at the beginning so that while removal, portchannels are removed first."""
        portchannels = [i for i in interfaces if i.startswith('PortChannel')]
        ports = [i for i in interfaces if not i.startswith('PortChannel')]
        return portchannels + ports

    # -------------------------------------------------------------------------
    # Group Management
    # -------------------------------------------------------------------------

    def create_group(
        self,
        name: str,
        num_uplinks: int = 2,
        num_downlinks: int = 1,
        min_uplinks: int = 1,
        startup_delay: int = 10,
        description: str = '',
        uplinks_down: bool = False,
        uplinks: Optional[List[str]] = None,
        downlinks: Optional[List[str]] = None
    ) -> None:
        """
        Create a monitor link group.

        Allocates interfaces from the available pool using IntfMgr,
        or uses pre-allocated interfaces if provided.

        Args:
            name: Group name
            num_uplinks: Number of uplinks to allocate (default: 2, ignored if uplinks provided)
            num_downlinks: Number of downlinks to allocate (default: 1, ignored if downlinks provided)
            min_uplinks: Minimum uplinks threshold (default: 1)
            startup_delay: Startup delay in seconds (default: 10)
            description: Optional group description
            uplinks_down: If True, shut down uplinks before creating group (default: False)
            uplinks: Pre-allocated uplink interfaces (for sharing across groups)
            downlinks: Pre-allocated downlink interfaces (for sharing across groups)
        """
        suffix = " with DOWN uplinks" if uplinks_down else ""

        # Use provided interfaces or allocate new ones
        if uplinks is None:
            uplinks = self.intf_mgr.allocate(num_uplinks, mode=self.mode)
        if downlinks is None:
            downlinks = self.intf_mgr.allocate(num_downlinks, mode=self.mode)

        logger.info(
            f"[ACTION] Creating group '{name}'{suffix} [mode={self.mode}]: "
            f"uplinks={uplinks}, downlinks={downlinks}, "
            f"min_uplinks={min_uplinks}, startup_delay={startup_delay}"
        )
        logger.debug(f"[ACTION] Allocated uplinks={uplinks}, downlinks={downlinks}")

        # Optionally shut down uplinks before creating the group
        if uplinks_down:
            for intf in uplinks:
                logger.info(f"[ACTION] Pre-shutting down uplink {intf}")
                self.duthandler.shutdown_port(intf)

        # Track in groups
        self.groups.add(name, uplinks, downlinks, min_uplinks, startup_delay, description)

        # Build config and load on DUT
        config = {
            "MONITOR_LINK_GROUP": {
                name: {
                    "uplinks": uplinks,
                    "downlinks": downlinks,
                    "min-uplinks": str(min_uplinks),
                    "startup-delay": str(startup_delay),
                }
            }
        }
        if description:
            config["MONITOR_LINK_GROUP"][name]["description"] = description
        self.duthandler.load_config(config)
        logger.info(f"[ACTION] Group '{name}' created{suffix}")

    def remove_group(self, name: str, release_interfaces: bool = True) -> None:
        """
        Remove a monitor link group.

        Brings interfaces back up and optionally releases them to the pool.

        Args:
            name: Group name
            release_interfaces: If True (default), release interfaces back to pool.
                               Set to False when interfaces are shared across groups.
        """
        logger.info(f"[ACTION] Removing group '{name}'")

        # Get interfaces to release before removing from tracking
        group = self.groups.get(name)
        interfaces = group['uplinks'] + group['downlinks']

        # Delete group from CONFIG_DB
        self.duthandler.del_configdb('MONITOR_LINK_GROUP', name)

        # Remove from tracking
        self.groups.remove(name)

        # Bring interfaces back up before releasing to pool
        for intf in interfaces:
            self.duthandler.startup_port(intf)

        # Release interfaces back to pool (unless shared)
        if release_interfaces:
            self.intf_mgr.release(*interfaces)
        logger.info(f"[ACTION] Group '{name}' removed (release_interfaces={release_interfaces})")

    def add_uplink(self, group_name: str, num_uplinks: int = 1) -> None:
        """
        Add uplink(s) to a group.

        Allocates interface(s) from available pool based on handler mode.

        Args:
            group_name: Group name
            num_uplinks: Number of uplinks to add (default: 1)
        """
        logger.info(f"[ACTION] Adding {num_uplinks} uplink(s) to group '{group_name}'")
        new_interfaces = self.intf_mgr.allocate(num_uplinks, mode=self.mode)
        current_uplinks = self.groups.get(group_name)['uplinks']
        all_uplinks = self._sort_portchannels_first(current_uplinks + new_interfaces)

        self._set_links(group_name, 'uplinks', all_uplinks)

        for interface in new_interfaces:
            self.groups.add_uplink(group_name, interface)
        logger.debug(f"[ACTION] Added uplinks: {new_interfaces}")

    def remove_uplink(self, group_name: str, index: int) -> None:
        """
        Remove an uplink from a group by index.

        Releases interface back to the pool.

        Args:
            group_name: Group name
            index: Index of uplink to remove
        """
        current_uplinks = self.groups.get(group_name)['uplinks']
        interface = current_uplinks[index]
        logger.info(f"[ACTION] Removing uplink[{index}]={interface} from group '{group_name}'")
        remaining_uplinks = current_uplinks[:index] + current_uplinks[index + 1:]

        self._set_links(group_name, 'uplinks', remaining_uplinks)
        self.groups.remove_uplink(group_name, interface)
        self.intf_mgr.release(interface)

    def add_downlink(self, group_name: str, num_downlinks: int = 1) -> None:
        """
        Add downlink(s) to a group.

        Allocates interface(s) from available pool based on handler mode.

        Args:
            group_name: Group name
            num_downlinks: Number of downlinks to add (default: 1)
        """
        logger.info(f"[ACTION] Adding {num_downlinks} downlink(s) to group '{group_name}'")
        new_interfaces = self.intf_mgr.allocate(num_downlinks, mode=self.mode)
        current_downlinks = self.groups.get(group_name)['downlinks']
        all_downlinks = self._sort_portchannels_first(current_downlinks + new_interfaces)

        self._set_links(group_name, 'downlinks', all_downlinks)

        for interface in new_interfaces:
            self.groups.add_downlink(group_name, interface)
        logger.debug(f"[ACTION] Added downlinks: {new_interfaces}")

    def remove_downlink(self, group_name: str, index: int) -> str:
        """
        Remove a downlink from a group by index.

        Releases interface back to the pool.

        Args:
            group_name: Group name
            index: Index of downlink to remove

        Returns:
            The removed interface name
        """
        current_downlinks = self.groups.get(group_name)['downlinks']
        interface = current_downlinks[index]
        logger.info(f"[ACTION] Removing downlink[{index}]={interface} from group '{group_name}'")
        remaining_downlinks = current_downlinks[:index] + current_downlinks[index + 1:]

        self._set_links(group_name, 'downlinks', remaining_downlinks)
        self.groups.remove_downlink(group_name, interface)
        self.intf_mgr.release(interface)
        return interface

    def get_downlinks(self, group_name: str) -> List[str]:
        """Get the list of downlink interfaces for a group."""
        return self.groups.get(group_name)['downlinks']

    def _set_links(self, group_name: str, link_type: str, links: List[str]) -> None:
        """Apply a patch to set uplinks or downlinks on a group."""
        patch = [
            {
                "op": "replace",
                "path": f"/MONITOR_LINK_GROUP/{group_name}/{link_type}",
                "value": links
            }
        ]
        self.duthandler.patch_config(patch)

    # -------------------------------------------------------------------------
    # Port State Management
    # -------------------------------------------------------------------------

    def _set_port_state(
        self, group_name: str, link_type: str, index: int, state: str
    ) -> None:
        """
        Set a port to UP or DOWN by index.

        Args:
            group_name: Group name
            link_type: 'uplinks' or 'downlinks'
            index: Interface index within the link type
            state: 'up' or 'down'
        """
        interfaces = self.groups.get(group_name)[link_type]
        interface = interfaces[index]
        link_label = 'uplink' if link_type == 'uplinks' else 'downlink'
        logger.info(
            f"[ACTION] Set {link_label}[{index}]={interface} {state.upper()} "
            f"in group '{group_name}'"
        )
        if state == 'up':
            self.duthandler.startup_port(interface)
        else:
            self.duthandler.shutdown_port(interface)

    def set_uplink_up(self, group_name: str, index: int) -> None:
        """Set an uplink to UP by index."""
        self._set_port_state(group_name, 'uplinks', index, 'up')

    def set_uplink_down(self, group_name: str, index: int) -> None:
        """Set an uplink to DOWN by index."""
        self._set_port_state(group_name, 'uplinks', index, 'down')

    def set_downlink_up(self, group_name: str, index: int) -> None:
        """Set a downlink to UP by index."""
        self._set_port_state(group_name, 'downlinks', index, 'up')

    def set_downlink_down(self, group_name: str, index: int) -> None:
        """Set a downlink to DOWN by index."""
        self._set_port_state(group_name, 'downlinks', index, 'down')

    # -------------------------------------------------------------------------
    # Verification
    # -------------------------------------------------------------------------

    def verify(
        self,
        group_name: str,
        expected_params: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Verify monitor link group state.

        Polls STATE_DB until verification passes (timeout = startup-delay + 1).

        Args:
            group_name: Group name
            expected_params: Expected state parameters
        """
        self.verifier.verify(group_name, expected_params)

    def verify_not_exists(self, group_name: str, timeout: int = 30) -> None:
        """
        Verify that a group does not exist.

        Polls until group is deleted from both CONFIG_DB and STATE_DB.

        Args:
            group_name: Group name
            timeout: Max seconds to wait.
        """
        self.verifier.verify_not_exists(group_name, timeout=timeout)

    def verify_link_up(self, interface: str) -> None:
        """
        Verify that a link is operationally up.

        Args:
            interface: Interface name (e.g., 'Ethernet0')
        """
        self.verifier.verify_link_state(interface, 'up')
