"""
Route manipulation helper functions for SONiC tests.
"""

import json
import logging

logger = logging.getLogger(__name__)


def add_static_route_to_dut(duthost, prefix, nexthop, vrf=None):
    """
    Add static route on the DUT via config command.

    Args:
        duthost: DUT host object
        prefix: Route prefix (e.g., "192.168.100.0/24" or "2001:db8:100::/64")
        nexthop: Next hop IP address
        vrf: VRF name (optional, defaults to None for default VRF)
              This specifies the VRF where the route will be added (route VRF),
              not the nexthop VRF.
    """
    nexthop = nexthop.split('/')[0]  # Strip prefix if present
    if vrf:
        cmd = 'config route add prefix vrf {} {} nexthop {}'.format(vrf, prefix, nexthop)
    else:
        cmd = 'config route add prefix {} nexthop {}'.format(prefix, nexthop)
    duthost.shell(cmd)
    vrf_str = f" in VRF {vrf}" if vrf else ""
    logger.info(f"Added static route: {prefix} via {nexthop}{vrf_str}")


def del_static_route_from_dut(duthost, prefix, nexthop, vrf=None):
    """
    Delete static route from the DUT via config command.

    Args:
        duthost: DUT host object
        prefix: Route prefix (e.g., "192.168.100.0/24" or "2001:db8:100::/64")
        nexthop: Next hop IP address
        vrf: VRF name (optional, defaults to None for default VRF)
              This specifies the VRF where the route exists (route VRF),
              not the nexthop VRF.
    """
    nexthop = nexthop.split('/')[0]  # Strip prefix if present
    if vrf:
        cmd = 'config route del prefix vrf {} {} nexthop {}'.format(vrf, prefix, nexthop)
    else:
        cmd = 'config route del prefix {} nexthop {}'.format(prefix, nexthop)
    duthost.shell(cmd)
    vrf_str = f" in VRF {vrf}" if vrf else ""
    logger.info(f"Deleted static route: {prefix} via {nexthop}{vrf_str}")


def get_route_count(duthost):
    """
    Get route count using SONiC CLI commands.

    Args:
        duthost: DUT host object

    Returns:
        tuple: (ipv4_count, ipv6_count) - Number of IPv4 and IPv6 routes
    """

    def _get_count(v6=False):
        af = "ipv6" if v6 else "ip"
        result = duthost.shell(f'show {af} route summary json', module_ignore_errors=True)
        if result['rc'] == 0:
            try:
                # SONiC CLI prepends namespace (e.g., ":" or "asic0:") before JSON
                # Strip everything before the first '{' to get valid JSON
                stdout = result['stdout']
                json_start = stdout.find('{')
                data = json.loads(stdout[json_start:])
                return data.get('routesTotal', 0)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse IPv4 route count: {e}")
        return 0

    return _get_count(), _get_count(v6=True)
