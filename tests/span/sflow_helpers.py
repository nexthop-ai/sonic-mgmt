'''
Helper functions for mirrored sFlow (MIRROR_SESSION type SFLOW) tests.

A mirrored sFlow session samples packets on its source port in hardware and sends the
samples as sFlow datagrams to dst_ip. The tests capture those datagrams on the PTF ports the
collector route points at, then decode them with sflowtool.
'''

import ast
import json
import logging
import random
import time

import ptf.testutils as testutils
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from erspan_helpers import ERSPAN_SRC_IP, ERSPAN_DST_IP, remove_mirror_session, send_batched

logger = logging.getLogger(__name__)

MIRRORED_SFLOW_SESSION_NAME = "sflow_sample_trunc"
SFLOW_DSCP = "8"
SFLOW_TTL = "64"
SFLOW_QUEUE = "0"
MIRRORED_SFLOW_DEFAULT_DIRECTION = "rx"
SFLOW_UDP_SRC_PORT = 6343
SFLOW_DEFAULT_UDP_DST_PORT = 6343

SFLOW_DRAIN_SECONDS = 5

# 1000 samples keeps the count within +/-10% at ~3 sigma while PTF sends at a few hundred pps
SFLOW_NUM_SAMPLES = 1000
SFLOW_COUNT_TOLERANCE = 0.10

SFLOWTOOL = "/usr/local/bin/sflowtool"


def create_mirrored_sflow_session_config(duthost, session_name, src_ip, dst_ip, source_port=None,
                                         direction=None, sample_rate=None, truncate_size=None,
                                         udp_dst_port=None, module_ignore_errors=False):
    '''
    Create a mirrored sFlow session via 'config mirror_session sflow add'.

    A leftover session with the same name is removed first. Options set to None are left
    off the command line. Returns the duthost.command result dict.
    '''
    if direction is not None and source_port is None:
        raise ValueError('direction is only applied together with source_port; pass source_port too')
    remove_mirror_session(duthost, session_name)
    cmd = 'config mirror_session sflow add {} {} {} {} {}'.format(
        session_name, src_ip, dst_ip, SFLOW_DSCP, SFLOW_TTL)
    if source_port is not None:
        # queue is positional before src_port
        cmd += ' {} {}'.format(SFLOW_QUEUE, source_port)
        if direction is not None:
            cmd += ' {}'.format(direction)
    if sample_rate is not None:
        cmd += ' --sample_rate {}'.format(sample_rate)
    if truncate_size is not None:
        cmd += ' --truncate_size {}'.format(truncate_size)
    if udp_dst_port is not None:
        cmd += ' --udp_dst_port {}'.format(udp_dst_port)
    return duthost.command(cmd, module_ignore_errors=module_ignore_errors)


def get_mirror_session_status(duthost, session_name):
    '''Return STATE_DB MIRROR_SESSION_TABLE status (empty string when unset).'''
    return duthost.shell(
        "sonic-db-cli STATE_DB HGET 'MIRROR_SESSION_TABLE|{}' status".format(session_name),
        module_ignore_errors=True)['stdout'].strip()


def wait_mirror_session_active(duthost, session_name, timeout=30):
    '''Return True once STATE_DB reports the session active, False on timeout.'''
    return wait_until(timeout, 2, 0, lambda: get_mirror_session_status(duthost, session_name) == 'active')


def get_port_ifindexes(duthost, ports):
    '''Return {port: ifIndex sFlow reports for it} for each port in `ports`.'''
    cmd = ("python3 -c \"from sonic_py_common import port_util; import sys; "
           "[print(p, port_util.get_index_from_str(p)) for p in sys.argv[1:]]\" {}")
    output = duthost.shell(cmd.format(' '.join(ports)))['stdout']
    return {name: int(index) for name, index in (line.split() for line in output.splitlines())}


def send_probes(ptfadapter, inject_index, probe, count, pktlen=100, eth_dst=None):
    '''
    Send `count` copies of a TCP probe from PTF port `inject_index`.

    `probe` holds the tag that marks this test's samples: ip_src, ip_dst and tcp_sport.
    Returns the probe bytes, so callers can compare sampled headers with them.
    '''
    pkt = testutils.simple_tcp_packet(
        pktlen=pktlen,
        eth_src=ptfadapter.dataplane.get_mac(0, inject_index),
        eth_dst=eth_dst or probe['eth_dst'],
        ip_src=probe['ip_src'],
        ip_dst=probe['ip_dst'],
        tcp_sport=probe['tcp_sport'])
    send_batched(ptfadapter, inject_index, pkt, count)
    logger.info("Sent %d probes (pktlen=%d, src=%s, sport=%d) from PTF port %d",
                count, pktlen, probe['ip_src'], probe['tcp_sport'], inject_index)
    return bytes(pkt)


def ptf_tx_packets(ptfhost, ptf_index):
    '''Return the TX packet counter of PTF port `ptf_index`.'''
    return int(ptfhost.shell("cat /sys/class/net/eth{}/statistics/tx_packets".format(ptf_index))['stdout'])


def wait_ptf_sent(ptfhost, ptf_index, tx_start, count):
    '''
    Wait until PTF port `ptf_index` has sent `count` packets since its TX counter read `tx_start`.

    ptfadapter returns once packets are queued to ptf_nn_agent, which drains at a few hundred
    pps, so a capture stopped right after the send misses most probes.
    '''
    timeout = 60 + count // 100
    pytest_assert(wait_until(timeout, 2, 0, lambda: ptf_tx_packets(ptfhost, ptf_index) - tx_start >= count),
                  "PTF port {} did not send {} probes within {}s".format(ptf_index, count, timeout))


def make_probe(eth_dst, ip_dst="192.0.2.1"):
    '''
    Return a probe with a random tag: ip_src and tcp_sport identify this test's samples
    in a capture that also holds background traffic and earlier sessions' samples.
    '''
    return {
        'eth_dst': eth_dst,
        'ip_src': "10.123.{}.{}".format(random.randint(0, 255), random.randint(1, 254)),
        'ip_dst': ip_dst,
        'tcp_sport': random.randint(10000, 60000),
    }


def mirrored_sflow_view(mirrored_sflow_ports, **fields):
    '''
    Return the dict the SFLOW tests use for a session on the source port; `fields` set the
    session-specific values (session_name, sample_rate, truncate_size, udp_dst_port, direction).

    RX probes go to the router MAC and are routed to the collector PortChannel. TX probes,
    injected on tx_ingress, are routed out the source port.
    '''
    ports = mirrored_sflow_ports
    src_port = ports['source']['name']
    view = {
        'source_port': src_port,
        'source_index': ports['source']['index'],
        'source_ifindex': ports['ifindex'].get(src_port),
        'collector_indices': ports['collector']['indices'],
        'tx_ingress_index': ports['tx_ingress']['index'] if ports['tx_ingress'] else None,
        'router_mac': ports['router_mac'],
        'src_ip': ERSPAN_SRC_IP,
        'dst_ip': ERSPAN_DST_IP,
        'udp_dst_port': SFLOW_DEFAULT_UDP_DST_PORT,
        'probe': make_probe(ports['router_mac'], ip_dst=ports['route_dst_ip']),
        'tx_probe': make_probe(ports['router_mac'], ip_dst=ports['source']['peer_addr']),
    }
    view.update(fields)
    return view


def _parse_sflowtool_line(line):
    # sflowtool -j emits one JSON object per datagram. Older builds emit Python-style
    # literals, which is why tests/sflow parses with ast.literal_eval.
    try:
        return json.loads(line)
    except ValueError:
        return ast.literal_eval(line)


class SflowCapture(object):
    '''
    Capture sFlow datagrams on PTF ports while the block runs, then decode them.

    with SflowCapture(ptfhost, ptf_indices, udp_dst_port) as cap:
        send_probes(...)
    cap.datagrams     # decoded sflowtool JSON, one dict per datagram
    cap.src_port_ok   # every captured datagram had UDP source port 6343

    The collector route can point at a PortChannel, and the LAG hash picks the member, so
    each member port gets its own tcpdump. tcpdump -U writes each packet as it arrives;
    without it the last few datagrams sit in the buffer when tcpdump stops and are lost.
    '''

    def __init__(self, ptfhost, ptf_indices, udp_dst_port=SFLOW_DEFAULT_UDP_DST_PORT,
                 drain=SFLOW_DRAIN_SECONDS):
        self.ptfhost = ptfhost
        self.ptf_indices = list(ptf_indices)
        self.udp_dst_port = udp_dst_port
        self.drain = drain
        self.pcaps = {index: "/tmp/sflow_mirror_eth{}_{}.pcap".format(index, udp_dst_port)
                      for index in self.ptf_indices}
        self.datagrams = []
        self.src_port_ok = False
        self._pids = []

    def __enter__(self):
        for index, pcap in self.pcaps.items():
            self.ptfhost.shell("rm -f {}".format(pcap))
            self._pids.append(self.ptfhost.shell(
                "nohup tcpdump -U -i eth{} -w {} 'udp dst port {}' >/dev/null 2>&1 & echo $!".format(
                    index, pcap, self.udp_dst_port))['stdout'].strip())
        # Let tcpdump open the interfaces before traffic starts
        time.sleep(2)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        time.sleep(self.drain)
        self.ptfhost.shell("kill -INT {} ; sleep 1".format(' '.join(self._pids)), module_ignore_errors=True)
        if exc_type is not None:
            return False
        total = from_6343 = 0
        for pcap in self.pcaps.values():
            output = self.ptfhost.shell("{} -j -r {}".format(SFLOWTOOL, pcap))['stdout']
            self.datagrams += [_parse_sflowtool_line(line) for line in output.splitlines()
                               if line.startswith('{')]
            total += int(self.ptfhost.shell(
                "tcpdump -nr {} 2>/dev/null | wc -l".format(pcap))['stdout'])
            from_6343 += int(self.ptfhost.shell(
                "tcpdump -nr {} 'udp src port {}' 2>/dev/null | wc -l".format(
                    pcap, SFLOW_UDP_SRC_PORT))['stdout'])
        self.src_port_ok = total == from_6343
        logger.info("Captured %d sFlow datagrams on PTF ports %s (udp dst %d, %d from src port %d)",
                    len(self.datagrams), self.ptf_indices, self.udp_dst_port, from_6343,
                    SFLOW_UDP_SRC_PORT)
        return False


def probe_samples(datagrams, probe):
    '''
    Return the flow samples whose sampled header carries this test's probe tag.

    The tag drops background traffic (BGP, LLDP) and samples a previous session left in
    the datagram builder. Each result is the sflowtool flow sample with its HEADER
    element under 'header' and the datagram's agent added.
    '''
    matched = []
    for datagram in datagrams:
        for sample in datagram.get('samples', []):
            if sample.get('sampleType') != 'FLOWSAMPLE':
                continue
            for element in sample.get('elements', []):
                if element.get('flowSampleType') != 'HEADER':
                    continue
                if (element.get('srcIP') == probe['ip_src'] and
                        element.get('TCPSrcPort') == str(probe['tcp_sport'])):
                    matched.append(dict(sample, header=element,
                                        agent=datagram.get('agent')))
    return matched


def header_bytes(sample):
    '''Return the sampled header of a flow sample as bytes.'''
    hex_str = sample['header'].get('headerBytes', '')
    return bytes(bytearray(int(b, 16) for b in hex_str.split('-') if b))


def assert_sample_count(count, expected, tolerance=SFLOW_COUNT_TOLERANCE, label=None):
    '''Assert `count` is within expected +/- tolerance.'''
    low = int(expected * (1 - tolerance))
    high = int(expected * (1 + tolerance))
    msg = "Sample count {} outside expected range [{}, {}]".format(count, low, high)
    if label:
        msg = "{}: {}".format(label, msg)
    pytest_assert(low <= count <= high, msg)


def assert_sample_fields(samples, sample_rate=None, input_port=None, output_port=None):
    '''Assert every sample reports the given sampling rate and input/output ifIndex.'''
    for sample in samples:
        if sample_rate is not None:
            pytest_assert(int(sample['meanSkipCount']) == int(sample_rate),
                          "Sample rate {} != configured {}".format(sample['meanSkipCount'], sample_rate))
        if input_port is not None:
            pytest_assert(int(sample['inputPort']) == int(input_port),
                          "Input ifIndex {} != expected {}".format(sample['inputPort'], input_port))
        if output_port is not None:
            pytest_assert(int(sample['outputPort']) == int(output_port),
                          "Output ifIndex {} != expected {}".format(sample['outputPort'], output_port))


def assert_datagram_headers(datagrams, agent):
    '''
    Assert every datagram from `agent` has version 5, declares as many samples as it carries,
    and that each sub-agent's datagram sequence numbers have no gaps or repeats.

    Capture start and drain cut datagrams at both ends, so the sequence need not start at 1.
    '''
    datagrams = [d for d in datagrams if d.get('agent') == agent]
    pytest_assert(datagrams, "No datagrams from agent {}".format(agent))
    seqs = {}
    for datagram in datagrams:
        pytest_assert(str(datagram.get('datagramVersion')) == '5',
                      "Datagram version {} != 5".format(datagram.get('datagramVersion')))
        pytest_assert(int(datagram.get('samplesInPacket', -1)) == len(datagram.get('samples', [])),
                      "Datagram declares {} samples but carries {}".format(
                          datagram.get('samplesInPacket'), len(datagram.get('samples', []))))
        seqs.setdefault(datagram.get('agentSubId'), []).append(int(datagram['packetSequenceNo']))
    for sub_agent, seq in seqs.items():
        seq.sort()
        pytest_assert(seq == list(range(seq[0], seq[0] + len(seq))),
                      "Sub-agent {} datagram sequence numbers have gaps or repeats: {}".format(sub_agent, seq))


def assert_flow_sample_source_fields(samples, source_ifindex):
    '''Assert each flow sample's source_id is the source port, it has no drops, and its header is Ethernet.'''
    for sample in samples:
        pytest_assert(sample.get('sourceId') == '0:{}'.format(source_ifindex),
                      "source_id {} != 0:{}".format(sample.get('sourceId'), source_ifindex))
        pytest_assert(int(sample.get('dropEvents', -1)) == 0,
                      "Sample drops {} != 0".format(sample.get('dropEvents')))
        pytest_assert(sample['header'].get('headerProtocol') == '1',
                      "Header protocol {} != 1 (Ethernet)".format(sample['header'].get('headerProtocol')))
