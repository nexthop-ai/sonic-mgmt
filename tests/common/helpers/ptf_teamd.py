"""PTF teamd LAG teardown helpers."""

import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)


def kill_ptf_teamd(ptfhost, bond_port):
    """Kill teamd for bond_port, delete the netdev, and drop leftover pid/sock.

    A leftover daemon owns /var/run/teamd/<bond_port>.sock, so the next
    'teamd -d' cannot bind it.

    Args:
        ptfhost: PTF host object
        bond_port: teamd LAG name (e.g. bond54)
    """
    ptfhost.shell("teamd -k -t {}".format(bond_port), module_ignore_errors=True)
    ptfhost.shell("ip link del {}".format(bond_port), module_ignore_errors=True)
    ptfhost.shell("rm -f /var/run/teamd/{0}.pid /var/run/teamd/{0}.sock".format(bond_port),
                  module_ignore_errors=True)


class PtfTeamdBondRegistry(object):
    """Bonds one suite started: bond_name -> member eth.

    Teardown only cleans these; it does not scan the PTF for other suites.
    """

    def __init__(self, suite):
        self.suite = suite
        self._bonds = OrderedDict()

    def register(self, bond_port, member):
        self._bonds[bond_port] = member

    def forget(self, bond_port):
        self._bonds.pop(bond_port, None)

    def cleanup(self, ptfhost, remove_bond):
        leftover = list(self._bonds.items())
        if not leftover:
            return
        logger.warning("PTF leftover teamd LAGs from %s: %s",
                       self.suite, [bond for bond, _ in leftover])
        for bond_port, member in leftover:
            remove_bond(ptfhost, bond_port, member)
