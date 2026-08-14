#!/usr/bin/python

import logging
import subprocess
import traceback

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = '''
module: fp_veth_port
version_added: "0.1"
short_description: Get/Create/Remove DUT front panel ports on a fabric-attached test server
description:
    - The counterpart of the vlan_port module for a test server that reaches the DUT over a
      VXLAN fabric instead of a cabled VLAN trunk. There is no trunk NIC to hang
      "<external_port>.<vlan>" subinterfaces off, so each DUT front panel port is represented
      by a veth pair "<prefix><vni>" / "<prefix><vni>h". The h side is tc-redirected to the
      vxlan netdev carrying that port, and the other side is what vm_topology puts in its OVS
      fp-bridge. Naming the veth after its VNI keeps it unique across DUTs and testbeds, so
      two DUTs sharing a test server cannot collide.
    - tc rather than bridging because a Linux bridge will not forward LACP
      (01:80:c2:00:00:02 is kernel-restricted) and OVS cannot own a kernel vxlan netdev
      (UDP 4789 clash). tc ingress runs before bridging, so the vxlan netdev stays in its
      own bridge and FRR keeps advertising the L2VNI.
    - The vxlan netdevs themselves are provisioned with the test server (netplan), not here,
      and their MTU is taken as the intended inner MTU: the veths inherit it, so the tunnels
      must be set to the payload size the DUT sends, not to the underlay size.
    - Each DUT port is paired with its tunnel by VNI. VNIs are assigned per testbed and
      recorded there, not computed, and prepare publishes the resolved map as dut_fp_vnis
      keyed on the port index device_vlan_map_list uses. Nothing here infers anything from
      the numbers: they need not be contiguous, ordered, or related to the port.
options:
    - cmd:       create | remove | list
    - fp_prefix: name prefix for the veths, e.g. "fp"
    - vlan_ids:  {port_index: vlan_id} for this DUT, used for which ports to set up
    - vnis:      {port_index: vni} for this DUT, from the dut_fp_vnis host var
'''

EXAMPLES = '''
- name: Set up DUT front panel veths for a fabric-attached test server
  fp_veth_port:
    fp_prefix: "{{ dut_fp_prefix }}"
    vlan_ids: "{{ device_vlan_map_list[dut_name] }}"
    vnis: "{{ hostvars[dut_name].dut_fp_vnis }}"
    cmd: "create"
'''

CMD_DEBUG_FNAME = '/tmp/fp_veth_port.cmds.txt'


logging.basicConfig(
    filename=CMD_DEBUG_FNAME,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s"
)


class FpVethPort(object):
    def __init__(self, fp_prefix, vlan_ids, vnis):
        self.fp_prefix = fp_prefix
        # Module args arrive as JSON, so dict keys are strings whichever type the playbook
        # used. Normalise both sides so a port index matches its VNI either way.
        self.vnis = {str(k): int(v) for k, v in (vnis or {}).items()}
        self.port_indexes = [str(k) for k in vlan_ids]
        missing = [i for i in self.port_indexes if i not in self.vnis]
        if missing:
            raise Exception(
                "no VNI for DUT port index %s. prepare publishes dut_fp_vnis for every front "
                "panel port that has one assigned, so a gap here means the port was cabled "
                "after the testbed's VNIs were assigned. Re-run 'nh tbadmin vni allocate' "
                "for this testbed and re-run prepare." % " ".join(missing))

        return

    def fp_name(self, vni):
        """Names the veth after the VNI it carries.

        Unique across DUTs and testbeds, so two DUTs sharing a test server cannot collide,
        and the veth reads back as its tunnel.
        """
        return "%s%d" % (self.fp_prefix, vni)

    def host_name(self, vni):
        return "%sh" % self.fp_name(vni)

    def fp_ports(self):
        """Returns {port_index: veth name}, the shape vlan_port returns."""
        return {port_index: self.fp_name(self.vnis[port_index]) for port_index in self.port_indexes}

    def vxlan_ifaces(self):
        """Returns {VNI: netdev name} for the test server's vxlan netdevs.

        Provisioned alongside the tunnels; this module only consumes them.
        """
        out = FpVethPort.cmd("ip -d -o link show type vxlan")
        by_vni = {}
        for row in out.split('\n'):
            if not row.strip():
                continue
            # "<idx>: <name>: <flags> ... vxlan id <vni> ..."
            terms = row.split()
            name = terms[1].rstrip(':').split('@')[0]
            if 'vxlan' in terms and 'id' in terms:
                vni = int(terms[terms.index('id', terms.index('vxlan')) + 1])
                by_vni[vni] = name
        return by_vni

    def paired_tunnels(self):
        """Returns [(veth, host veth, vxlan netdev)] for this DUT's front panel ports.

        Each port is looked up by the VNI carrying it, so the result does not depend on how
        many tunnels the test server has or what order they appear in. A tunnel that is not
        a DUT front panel port - a management port over the fabric, or a second DUT sharing
        this test server - is simply not asked for.
        """
        by_vni = self.vxlan_ifaces()
        pairs = []
        for port_index in self.port_indexes:
            vni = self.vnis[port_index]
            if vni not in by_vni:
                raise Exception(
                    "test server has no vxlan netdev for VNI %d, needed by DUT port index %s. "
                    "Tunnels are provisioned with the test server, not created here; it has "
                    "VNIs %s." % (vni, port_index, ",".join(str(v) for v in sorted(by_vni))))
            pairs.append((self.fp_name(vni), self.host_name(vni), by_vni[vni]))
        return pairs

    def verify_fp_ports(self):
        """Raises if any veth this DUT expects is absent.

        Called after create rather than from cmd=list, because get_dut_port.yml runs list
        before create_dut_port.yml has made them.
        """
        missing = [name for name in self.fp_ports().values() if not FpVethPort.iface_exists(name)]
        if missing:
            raise Exception(
                "front panel veths absent after create: %s. Without them vm_topology fails "
                "later with an opaque bind error." % " ".join(missing))

    def create_fp_ports(self):
        for fp, host, vxlan in self.paired_tunnels():
            if not FpVethPort.iface_exists(fp):
                FpVethPort.cmd('ip link add %s type veth peer name %s' % (fp, host))
            # A veth defaults to 1500, where the subinterface this replaces inherited
            # the trunk NIC's MTU. Match the tunnel, so provisioning owns the MTU.
            mtu = FpVethPort.iface_mtu(vxlan)
            if mtu:
                FpVethPort.cmd('ip link set %s mtu %s' % (fp, mtu))
                FpVethPort.cmd('ip link set %s mtu %s' % (host, mtu))
            FpVethPort.iface_up(fp)
            FpVethPort.iface_up(host)
            FpVethPort.iface_up(vxlan)

            # Recreate the qdisc so the filters below are the only ones present. Deleting a
            # missing ingress qdisc is not an error worth failing on.
            for iface in (vxlan, host):
                FpVethPort.cmd('tc qdisc del dev %s ingress' % iface, ignore_error=True)
                FpVethPort.cmd('tc qdisc add dev %s handle ffff: ingress' % iface)

            FpVethPort.redirect(vxlan, host)
            FpVethPort.redirect(host, vxlan)

        self.verify_fp_ports()

        return

    def remove_fp_ports(self):
        # Clear the tunnel side first so no redirect is left pointing at a deleted veth.
        # Best effort: if the tunnels no longer pair up, still remove the veths.
        try:
            for _, _, vxlan in self.paired_tunnels():
                FpVethPort.cmd('tc qdisc del dev %s ingress' % vxlan, ignore_error=True)
        except Exception as detail:
            logging.warning("skipping tunnel-side cleanup: %s", detail)

        for port_index in self.port_indexes:
            vni = self.vnis[port_index]
            fp, host = self.fp_name(vni), self.host_name(vni)
            for iface in (fp, host):
                if FpVethPort.iface_exists(iface):
                    FpVethPort.cmd('tc qdisc del dev %s ingress' % iface, ignore_error=True)
            # Deleting either end removes the pair.
            if FpVethPort.iface_exists(fp):
                FpVethPort.iface_down(fp)
                FpVethPort.cmd('ip link del %s' % fp)

        return

    @staticmethod
    def redirect(src, dst):
        FpVethPort.cmd(
            'tc filter add dev %s parent ffff: protocol all u32 match u8 0 0 '
            'action mirred egress redirect dev %s' % (src, dst))

    @staticmethod
    def iface_up(iface_name):
        return FpVethPort.cmd('ip link set %s up' % iface_name)

    @staticmethod
    def iface_down(iface_name):
        return FpVethPort.cmd('ip link set %s down' % iface_name)

    @staticmethod
    def iface_mtu(iface_name):
        """Returns the interface's MTU, or None if it cannot be read."""
        out = FpVethPort.cmd('ip -o link show dev %s' % iface_name, ignore_error=True)
        terms = out.split()
        if 'mtu' in terms:
            return terms[terms.index('mtu') + 1]
        return None

    @staticmethod
    def iface_exists(iface_name):
        out = FpVethPort.cmd('ip -o link show dev %s' % iface_name, ignore_error=True)
        return bool(out.strip())

    @staticmethod
    def cmd(cmdline, ignore_error=False):
        logging.debug("CMD: %s", cmdline)
        process = subprocess.Popen(  # nosemgrep: subprocess-shell-true
            cmdline, stdout=subprocess.PIPE,
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)  # nosemgrep: subprocess-shell-true
        stdout, stderr = process.communicate()
        ret_code = process.returncode

        if ret_code != 0 and not ignore_error:
            raise Exception("ret_code=%d, error message=%s. cmd=%s" %
                            (ret_code, stderr, cmdline))

        if ret_code == 0:
            logging.info("OUTPUT: %s", stdout)
        else:
            logging.error("ERR: %s", stderr)

        return stdout.decode('utf-8')


def main():

    module = AnsibleModule(argument_spec=dict(
        cmd=dict(required=True, choices=['create', 'remove', 'list']),
        fp_prefix=dict(required=True, type='str'),
        vlan_ids=dict(required=True, type='dict'),
        vnis=dict(required=True, type='dict'),
    ))

    # log separator
    logging.info(
        "--------------------------------------------------------------------")

    cmd = module.params['cmd']
    fp_prefix = module.params['fp_prefix']
    vlan_ids = module.params['vlan_ids']
    vnis = module.params['vnis']

    fvp = FpVethPort(fp_prefix, vlan_ids, vnis)
    try:
        if cmd == "create":
            fvp.create_fp_ports()
        elif cmd == "remove":
            fvp.remove_fp_ports()

        module.exit_json(changed=False, ansible_facts={
                         'dut_fp_ports': fvp.fp_ports()})
    except Exception as detail:
        module.fail_json(msg="ERROR: %s, TRACEBACK: %s" %
                         (repr(detail), traceback.format_exc()))


if __name__ == "__main__":
    main()
