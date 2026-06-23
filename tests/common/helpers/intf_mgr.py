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

from tests.common.helpers.sonic_db import SonicDbCli
from tests.common.platform.interface_utils import sort_ethernet_intfs
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)


class InsufficientInterfacesError(ValueError):
    """Raised when the DUT lacks enough usable interfaces/PortChannels for a request.

    This signals a topology/environment limitation (e.g. fabric topologies where
    nearly every front-panel port is a single-member PortChannel, leaving too few
    standalone Ethernet ports), not a product or test defect. Tests may catch it to
    skip rather than fail. Subclasses ValueError to stay backward-compatible with
    callers that catch ValueError.
    """


class IntfMgr:
    """
    Manages available interfaces on the DUT for test allocation.

    Tracks which Ethernet interfaces and PortChannels are available
    and which are allocated to tests.
    Attempts to bring up all interfaces and tracks those that come up.

    Args:
        duthost: The pytest duthost fixture
    """

    # Aggregate budget for bringing up all admin-down interfaces / PortChannels
    # in parallel. Real-hardware link negotiation can take several seconds per
    # port; the budget applies across the whole set, so every port gets the
    # full window to come up.
    _BRING_UP_TIMEOUT = 60
    _BRING_UP_POLL_INTERVAL = 2

    def __init__(self, duthost):
        self.duthost = duthost
        self._config_db = SonicDbCli(duthost, 'CONFIG_DB')
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
        - Skip PortChannel members. Their admin state is not independent of
          the LAG, so allocating them as standalone ports breaks tests that
          toggle them.
        - If already oper up, add to list.
        - Otherwise bring all candidates up at once and wait in parallel.
          Candidates include both admin-down ports and admin-up/oper-down
          ports (e.g. ports left flapping by a previous test).
        """
        pc_members = self._portchannel_members()
        interfaces = self.duthost.get_interfaces_status()
        ethernet_intfs = sort_ethernet_intfs(interfaces)

        up_set = set()
        candidates = []
        for intf in ethernet_intfs:
            if intf in pc_members:
                continue
            info = interfaces[intf]
            if info.get('oper') == 'up':
                up_set.add(intf)
            else:
                candidates.append(intf)

        if candidates:
            up_set.update(self._bring_up_in_parallel(candidates))

        # Filter the already-sorted source list to preserve ordering.
        up_interfaces = [intf for intf in ethernet_intfs if intf in up_set]

        logger.info(f"IntfMgr: {len(up_interfaces)} usable interfaces out of {len(ethernet_intfs)}")
        return up_interfaces

    def _portchannel_members(self) -> Set[str]:
        """Return the set of Ethernet ports that are members of any PortChannel.

        Reads CONFIG_DB's PORTCHANNEL_MEMBER table, whose keys have the form
        'PORTCHANNEL_MEMBER|<lag>|<member>'.
        """
        keys = self._config_db.get_keys(
            'PORTCHANNEL_MEMBER|*', raise_error_when_not_found=False
        )
        return {k.rsplit('|', 1)[-1] for k in keys}

    def _bring_up_in_parallel(self, intfs: List[str]) -> List[str]:
        """
        Bring up multiple interfaces in parallel.

        Issues a single ``no shutdown`` for all ``intfs`` and then polls
        ``show interfaces status`` until every interface is oper up or
        ``_BRING_UP_TIMEOUT`` elapses. Returns the subset that came up.

        Args:
            intfs: Interface names (Ethernet or PortChannel) to bring up.

        Returns:
            List of interfaces that came oper up before the deadline.
        """
        try:
            self.duthost.no_shutdown_multiple(intfs)
        except Exception as e:
            logger.debug(f"IntfMgr: parallel no_shutdown failed: {e}")
            return []

        pending = set(intfs)
        up: Set[str] = set()

        def _poll_oper_up() -> bool:
            try:
                interfaces = self.duthost.get_interfaces_status()
            except Exception as e:
                logger.debug(f"IntfMgr: get_interfaces_status failed during poll: {e}")
                return False
            now_up = {i for i in pending if interfaces.get(i, {}).get('oper') == 'up'}
            up.update(now_up)
            pending.difference_update(now_up)
            return not pending

        wait_until(self._BRING_UP_TIMEOUT, self._BRING_UP_POLL_INTERVAL, 0, _poll_oper_up)

        if pending:
            logger.debug(
                f"IntfMgr: {len(pending)} interfaces did not come up within "
                f"{self._BRING_UP_TIMEOUT}s: {sorted(pending)}"
            )
        return list(up)

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
            raise InsufficientInterfacesError(
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

        Mirrors :py:meth:`_load_interfaces`: oper-up PortChannels are kept,
        everything else (admin-down or admin-up/oper-down) is fed into the
        bulk bring-up path so flapping LAGs left over by a prior test can
        recover.
        """
        interfaces = self.duthost.get_interfaces_status()
        portchannel_intfs = sorted(
            [name for name in interfaces if name.startswith('PortChannel')],
            key=lambda x: int(x.replace('PortChannel', ''))
        )

        up_set = set()
        candidates = []
        for intf in portchannel_intfs:
            info = interfaces[intf]
            if info.get('oper') == 'up':
                up_set.add(intf)
            else:
                candidates.append(intf)

        if candidates:
            up_set.update(self._bring_up_in_parallel(candidates))

        # Filter the already-sorted source list to preserve ordering.
        up_portchannels = [pc for pc in portchannel_intfs if pc in up_set]

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
            raise InsufficientInterfacesError(
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
