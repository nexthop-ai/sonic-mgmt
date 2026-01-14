"""
MonitorLinkGroups - Manages monitor link group configurations.

Tracks all configured groups and provides methods to add/remove/query groups.

Schema:
-------
Each group has:
    {
        'uplinks': List[str],       # List of uplink interface names
        'downlinks': List[str],     # List of downlink interface names
        'startup-delay': str,       # Startup delay in seconds (as string)
        'min-uplinks': str,         # Minimum uplinks threshold (as string)
        'description': str          # Optional group description
    }
"""

from typing import Dict, Any, List


class MonitorLinkGroups:
    """
    Manages monitor link group configurations.

    Provides a central place to track all configured groups,
    add/remove groups, and query group configurations.

    Usage:
        groups = MonitorLinkGroups()
        groups.add('group-1', uplinks=['Ethernet0', 'Ethernet4'],
                   downlinks=['Ethernet8'], min_uplinks=1, startup_delay=5)
        config = groups.get('group-1')
        groups.remove('group-1')
    """

    def __init__(self):
        self._groups: Dict[str, Dict[str, Any]] = {}

    def add(
        self,
        name: str,
        uplinks: List[str],
        downlinks: List[str],
        min_uplinks: int = 1,
        startup_delay: int = 0,
        description: str = ''
    ) -> None:
        """
        Add a new monitor link group.

        Args:
            name: Group name
            uplinks: List of uplink interface names
            downlinks: List of downlink interface names
            min_uplinks: Minimum uplinks threshold (default: 1)
            startup_delay: Startup delay in seconds (default: 0)
            description: Optional group description (default: '')

        Raises:
            ValueError: If group already exists.
        """
        if name in self._groups:
            raise ValueError(f"Group '{name}' already exists")

        self._groups[name] = {
            'uplinks': list(uplinks),
            'downlinks': list(downlinks),
            'min-uplinks': str(min_uplinks),
            'startup-delay': str(startup_delay),
            'description': description,
        }

    def remove(self, name: str) -> None:
        """
        Remove a monitor link group.

        Args:
            name: Group name

        Raises:
            KeyError: If group does not exist.
        """
        if name not in self._groups:
            raise KeyError(f"Group '{name}' does not exist")
        del self._groups[name]

    def get(self, name: str) -> Dict[str, Any]:
        """
        Get configuration for a specific group.

        Args:
            name: Group name

        Returns:
            Group configuration dict.

        Raises:
            KeyError: If group does not exist.
        """
        if name not in self._groups:
            raise KeyError(f"Group '{name}' does not exist")
        return self._groups[name]

    def exists(self, name: str) -> bool:
        """Check if a group exists."""
        return name in self._groups

    def add_uplink(self, name: str, interface: str) -> None:
        """
        Add an uplink to a group.

        Args:
            name: Group name
            interface: Interface name to add

        Raises:
            KeyError: If group does not exist.
        """
        group = self.get(name)
        if interface in group['uplinks']:
            return
        group['uplinks'].append(interface)

    def remove_uplink(self, name: str, interface: str) -> None:
        """
        Remove an uplink from a group.

        Args:
            name: Group name
            interface: Interface name to remove

        Raises:
            KeyError: If group does not exist.
        """
        group = self.get(name)
        if interface not in group['uplinks']:
            return
        group['uplinks'].remove(interface)

    def add_downlink(self, name: str, interface: str) -> None:
        """
        Add a downlink to a group.

        Args:
            name: Group name
            interface: Interface name to add

        Raises:
            KeyError: If group does not exist.
        """
        group = self.get(name)
        if interface in group['downlinks']:
            return
        group['downlinks'].append(interface)

    def remove_downlink(self, name: str, interface: str) -> None:
        """
        Remove a downlink from a group.

        Args:
            name: Group name
            interface: Interface name to remove

        Raises:
            KeyError: If group does not exist.
        """
        group = self.get(name)
        if interface not in group['downlinks']:
            return
        group['downlinks'].remove(interface)
