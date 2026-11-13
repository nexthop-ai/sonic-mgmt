#!/usr/bin/env python3
"""Samples, once a second, what an EVPN-MH switch needs before its Ethernet segments should carry traffic, as JSON
lines: zebra's start time, Established BGP EVPN peers, EVPN route counts by type (1, 2, 4), the ASIC FDB entry of a
remote MAC and its bridge port type, ASIC bridge-port next hop groups, and the ES member's state.

    vxlan_evpn_mh_ready_sampler.py <remote mac> <ES member port> <output file>
"""

import json
import subprocess
import sys
import time


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def redis(db, *args):
    return sh("redis-cli -n {} {}".format(db, " ".join("'{}'".format(a) for a in args))).strip()


def sample(mac, port):
    t = time.time()
    s = {"t": round(t, 2)}
    pid = sh("docker exec bgp pgrep -x zebra").split()
    if pid:
        age = sh("docker exec bgp ps -o etimes= -p {}".format(pid[0])).strip()
        if age.isdigit():
            s["zebra_start"] = round(t - int(age))
    try:
        peers = json.loads(sh("docker exec bgp vtysh -c 'show bgp l2vpn evpn summary json'") or "{}").get("peers", {})
        s["evpn_est"] = sum(1 for p in peers.values() if p.get("state") == "Established")
        s["evpn_peers"] = len(peers)
    except ValueError:
        pass
    routes = sh("docker exec bgp vtysh -c 'show bgp l2vpn evpn route json'")
    s["routes"] = [routes.count('"[{}]:'.format(k)) for k in (1, 2, 4)]
    keys = sh("redis-cli -n 1 --scan --pattern '*SAI_OBJECT_TYPE_FDB_ENTRY:*{}*'".format(mac.upper())).split()
    s["fdb"] = len(keys)
    if len(keys) == 1:
        bp = redis(1, "HGET", keys[0], "SAI_FDB_ENTRY_ATTR_BRIDGE_PORT_ID")
        s["fdb_bp"] = redis(1, "HGET", "ASIC_STATE:SAI_OBJECT_TYPE_BRIDGE_PORT:" + bp, "SAI_BRIDGE_PORT_ATTR_TYPE")
    nhgs = sh("redis-cli -n 1 --scan --pattern 'ASIC_STATE:SAI_OBJECT_TYPE_NEXT_HOP_GROUP:*'").split()
    s["bp_nhg"] = sum(1 for k in nhgs
                      if redis(1, "HGET", k, "SAI_NEXT_HOP_GROUP_ATTR_TYPE") == "SAI_NEXT_HOP_GROUP_TYPE_BRIDGE_PORT")
    s["oper"] = redis(0, "HGET", "PORT_TABLE:" + port, "oper_status")
    try:
        with open("/sys/class/net/{}/proto_down".format(port)) as f:
            s["pd"] = int(f.read().strip())
    except (OSError, ValueError):
        pass
    return s


def main():
    mac, port, out = sys.argv[1:4]
    while True:
        try:
            line = json.dumps(sample(mac, port))
        except Exception as e:  # keep sampling through a restart
            line = json.dumps({"t": round(time.time(), 2), "error": str(e)})
        with open(out, "a") as f:
            f.write(line + "\n")
        time.sleep(1)


if __name__ == "__main__":
    main()
