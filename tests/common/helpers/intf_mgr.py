"""
IntfMgr - Interface Manager for tracking and allocating interfaces in tests.

Keeps track of usable Ethernet interfaces and PortChannels on the DUT
and their allocation status.
Attempts to bring up admin-down interfaces to maximize available ports.

Usage:
    intf_mgr = IntfMgr(duthost)         # Loads and brings up interfaces
    interfaces = intf_mgr.allocate(3)   # Get 3 available Ethernet interfaces
    intf_mgr.release(*interfaces)       # Return interfaces to pool

    # PortChannel support
    portchannels = intf_mgr.allocate_portchannels(2)  # Get 2 available PortChannels

    # Mixed allocation (1 PortChannel + rest Ethernet)
    mixed = intf_mgr.allocate(3, mode='mixed')  # Get 1 PortChannel + 2 Ethernet
"""

import logging
from typing import List, Set

from tests.common.platform.interface_utils import sort_ethernet_intfs
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)


class IntfMgr:
    """
    Manages available interfaces on the DUT for test allocation.

    Tracks which Ethernet interfaces and PortChannels are available
    and which are allocated to tests.
    Attempts to bring up all interfaces and tracks those that come up.

    Args:
        duthost: The pytest duthost fixture
    """

    def __init__(self, duthost):
        self.duthost = duthost
        # Ethernet interface tracking
        self._allocated: Set[str] = set()
        self._up_interfaces: List[str] = self._load_interfaces()
        # PortChannel tracking
        self._allocated_portchannels: Set[str] = set()
        self._up_portchannels: List[str] = self._load_portchannels()

    def _load_interfaces(self) -> List[str]:
        """
        Load usable Ethernet interfaces from DUT.

        For each Ethernet interface:
        - If already oper up, add to list
        - If admin down, try to bring it up and check if it comes up
        - Skip interfaces that fail to come up (likely no link partner)
        """
        interfaces = self.duthost.get_interfaces_status()
        ethernet_intfs = sort_ethernet_intfs(interfaces)

        up_interfaces = []
        for intf in ethernet_intfs:
            info = interfaces[intf]
            if info.get('oper') == 'up':
                up_interfaces.append(intf)
            elif info.get('admin') == 'down':
                # Try to bring it up
                if self._try_bring_up(intf):
                    up_interfaces.append(intf)

        logger.info(f"IntfMgr: {len(up_interfaces)} usable interfaces out of {len(ethernet_intfs)}")
        return up_interfaces

    def _try_bring_up(self, intf: str) -> bool:
        """
        Try to bring up an interface and verify it comes up.

        Args:
            intf: Interface name

        Returns:
            True if interface came up, False otherwise
        """
        try:
            self.duthost.no_shutdown(intf)
            # Wait briefly for oper status to become up
            if wait_until(1, 0.5, 0, self._is_oper_up, intf):
                logger.debug(f"IntfMgr: {intf} came up")
                return True
            else:
                logger.debug(f"IntfMgr: {intf} did not come up, skipping")
                return False
        except Exception as e:
            logger.debug(f"IntfMgr: Failed to bring up {intf}: {e}")
            return False

    def _is_oper_up(self, intf: str) -> bool:
        """Check if interface is operationally up."""
        interfaces = self.duthost.get_interfaces_status()
        return interfaces.get(intf, {}).get('oper') == 'up'

    def all(self) -> List[str]:
        """Get all operationally up interfaces."""
        return list(self._up_interfaces)

    def available(self) -> List[str]:
        """Get list of available (unallocated) up interfaces."""
        return [i for i in self._up_interfaces if i not in self._allocated]

    def allocated(self) -> List[str]:
        """Get list of allocated interfaces."""
        return [i for i in self._up_interfaces if i in self._allocated]

    def allocate(self, count: int = 1, mode: str = 'ports') -> List[str]:
        """
        Allocate interfaces from the available pool.

        Args:
            count: Number of interfaces to allocate (default: 1)
            mode: Allocation mode (default: 'ports')
                  'ports': All Ethernet interfaces
                  'mixed': 1 PortChannel + (count-1) Ethernet interfaces

        Returns:
            List of allocated interface names.

        Raises:
            ValueError: If not enough interfaces available or invalid mode.
        """
        if mode == 'mixed':
            return self._allocate_mixed(count)
        elif mode == 'ports':
            return self._allocate_ports(count)
        else:
            raise ValueError(f"Invalid mode '{mode}'. Must be 'ports' or 'mixed'")

    def _allocate_ports(self, count: int) -> List[str]:
        """Allocate Ethernet interfaces only."""
        avail = self.available()
        if len(avail) < count:
            raise ValueError(
                f"Not enough interfaces: requested {count}, available {len(avail)}"
            )

        allocated = avail[:count]
        self._allocated.update(allocated)
        return allocated

    def _allocate_mixed(self, count: int) -> List[str]:
        """Allocate 1 PortChannel + (count-1) Ethernet interfaces."""
        if count < 1:
            return []

        # Allocate 1 portchannel
        portchannels = self.allocate_portchannels(1)

        # Allocate remaining as Ethernet ports
        ports = []
        if count > 1:
            ports = self._allocate_ports(count - 1)

        return portchannels + ports

    def release(self, *interfaces: str) -> None:
        """
        Release interfaces back to the available pool.

        Automatically detects interface type (Ethernet or PortChannel)
        and releases to the appropriate pool. Interfaces that are not
        operationally up are removed from the pool entirely.

        Args:
            interfaces: Interface names to release.
        """
        for intf in interfaces:
            is_portchannel = intf.startswith('PortChannel')

            # Remove from allocated set
            if is_portchannel:
                self._allocated_portchannels.discard(intf)
            else:
                self._allocated.discard(intf)

            # Check if interface is up - if not, remove from pool
            if not self._is_oper_up(intf):
                if is_portchannel:
                    self._up_portchannels = [pc for pc in self._up_portchannels if pc != intf]
                else:
                    self._up_interfaces = [i for i in self._up_interfaces if i != intf]
                logger.warning(f"Interface '{intf}' is not oper up, removed from pool")

    # =========================================================================
    # PortChannel Management
    # =========================================================================

    def _load_portchannels(self) -> List[str]:
        """
        Load usable PortChannel interfaces from DUT.

        For each PortChannel interface:
        - If already oper up, add to list
        - If admin down, try to bring it up and check if it comes up
        - Skip PortChannels that fail to come up
        """
        interfaces = self.duthost.get_interfaces_status()
        portchannel_intfs = sorted(
            [name for name in interfaces if name.startswith('PortChannel')],
            key=lambda x: int(x.replace('PortChannel', ''))
        )

        up_portchannels = []
        for intf in portchannel_intfs:
            info = interfaces[intf]
            if info.get('oper') == 'up':
                up_portchannels.append(intf)
            elif info.get('admin') == 'down':
                # Try to bring it up
                if self._try_bring_up(intf):
                    up_portchannels.append(intf)

        logger.info(
            f"IntfMgr: {len(up_portchannels)} usable PortChannels "
            f"out of {len(portchannel_intfs)}"
        )
        return up_portchannels

    def all_portchannels(self) -> List[str]:
        """Get all operationally up PortChannels."""
        return list(self._up_portchannels)

    def available_portchannels(self) -> List[str]:
        """Get list of available (unallocated) up PortChannels."""
        return [pc for pc in self._up_portchannels if pc not in self._allocated_portchannels]

    def allocated_portchannels(self) -> List[str]:
        """Get list of allocated PortChannels."""
        return [pc for pc in self._up_portchannels if pc in self._allocated_portchannels]

    def allocate_portchannels(self, count: int = 1) -> List[str]:
        """
        Allocate PortChannels from the available pool.

        Args:
            count: Number of PortChannels to allocate (default: 1)

        Returns:
            List of allocated PortChannel names.

        Raises:
            ValueError: If not enough PortChannels available.
        """
        avail = self.available_portchannels()
        if len(avail) < count:
            raise ValueError(
                f"Not enough PortChannels: requested {count}, available {len(avail)}"
            )

        allocated = avail[:count]
        self._allocated_portchannels.update(allocated)
        return allocated

    def release_portchannels(self, *portchannels: str) -> None:
        """
        Release PortChannels back to the available pool.

        Args:
            portchannels: PortChannel names to release.
        """
        for pc in portchannels:
            self._allocated_portchannels.discard(pc)
