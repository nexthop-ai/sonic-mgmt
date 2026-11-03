import ast
import logging

import pytest

from tests.common.helpers.assertions import pytest_assert, pytest_require
from tests.common.utilities import wait_until
from erspan_helpers import (
    ERSPAN_SRC_IP,
    ERSPAN_DST_IP,
    collect_erspan_packets,
    create_erspan_session_config,
    assert_mirror_session_config_db_fields,
    mirror_session_config_db_exists,
)
from tests.everflow.everflow_test_utilities import BaseEverflowTest
from sflow_helpers import (
    SFLOW_NUM_SAMPLES,
    SflowCapture,
    create_mirrored_sflow_session_config,
    get_mirror_session_status,
    wait_mirror_session_active,
    mirrored_sflow_view,
    make_probe,
    send_probes,
    ptf_tx_packets,
    wait_ptf_sent,
    probe_samples,
    header_bytes,
    assert_sample_count,
    assert_sample_fields,
    assert_datagram_headers,
    assert_flow_sample_source_fields,
)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t2'),
    pytest.mark.disable_memory_utilization,
    pytest.mark.usefixtures('skip_if_mirrored_sflow_unsupported'),
]

DEFAULT_SAMPLE_RATE = 256
DEFAULT_TRUNCATE_SIZE = 256
CUSTOM_UDP_DST_PORT = 6400

# Truncation checks look at each sample, not the count, so a low rate keeps them short
TRUNCATION_SAMPLE_RATE = 10
TRUNCATION_PROBE_COUNT = 20000


def _capture_samples(ptfhost, ptfadapter, session, inject_index, probe=None, count=None,
                     pktlen=100, eth_dst=None):
    '''
    Send probes from `inject_index` while capturing on the collector port. Returns
    (matching samples, capture, probe bytes).
    '''
    probe = probe or session['probe']
    count = count if count is not None else SFLOW_NUM_SAMPLES * session['sample_rate']
    tx_start = ptf_tx_packets(ptfhost, inject_index)
    with SflowCapture(ptfhost, session['collector_indices'], session['udp_dst_port']) as capture:
        probe_bytes = send_probes(ptfadapter, inject_index, probe, count, pktlen=pktlen, eth_dst=eth_dst)
        wait_ptf_sent(ptfhost, inject_index, tx_start, count)
    samples = probe_samples(capture.datagrams, probe)
    logger.info("%d probe samples (rate 1:%s, sent %d)", len(samples), session['sample_rate'], count)
    return samples, capture, probe_bytes


def _create_active_session(duthost, mirror_session_cleanup, name, source_port, sample_rate,
                           direction='rx', expect_active=True):
    '''Create an SFLOW session to the collector and check whether it goes active.'''
    mirror_session_cleanup(name)
    create_mirrored_sflow_session_config(duthost, name, ERSPAN_SRC_IP, ERSPAN_DST_IP,
                                         source_port=source_port, direction=direction,
                                         sample_rate=sample_rate)
    active = wait_mirror_session_active(duthost, name, timeout=30 if expect_active else 15)
    pytest_assert(active == expect_active,
                  "SFLOW session {} on {}: expected active={}, STATE_DB status={!r}".format(
                      name, source_port, expect_active, get_mirror_session_status(duthost, name)))


def _pick_portchannel(mirrored_sflow_ports):
    '''Return (name, info) of a PortChannel other than the collector with at least 2 members.'''
    collector = mirrored_sflow_ports['collector']['portchannel']
    candidates = [(name, info) for name, info in mirrored_sflow_ports['portchannels'].items()
                  if name != collector and len(info['members']) >= 2]
    pytest_require(candidates, "Topology has no PortChannel with 2 or more members besides the collector")
    return candidates[0]


def _any_sampling_direction(erspan_capabilities):
    '''Return a direction the platform can sample: rx when it samples ingress, else tx.'''
    ingress = erspan_capabilities.get("PORT_INGRESS_SAMPLE_MIRROR_CAPABLE", "false").lower() == 'true'
    return 'rx' if ingress else 'tx'


def _inject(session, leg):
    '''Return (PTF port, probe) that triggers sampling on the RX or TX leg of the session.'''
    if leg == 'rx':
        return session['source_index'], session['probe']
    return session['tx_ingress_index'], session['tx_probe']


def _session_view(mirrored_sflow_session, **overrides):
    '''A copy of the mirrored_sflow_session dict with some fields replaced.'''
    view = dict(mirrored_sflow_session)
    view.update(overrides)
    return view


# ---------------------------------------------------------------------------
# Config and CLI
# ---------------------------------------------------------------------------

CREATE_CONFIG_DB_CASES = [
    pytest.param({"sample_rate": DEFAULT_SAMPLE_RATE}, False, id="sample_rate"),
    pytest.param({"sample_rate": DEFAULT_SAMPLE_RATE, "truncate_size": DEFAULT_TRUNCATE_SIZE},
                 True, id="sample_rate_and_truncate_size"),
    pytest.param({"sample_rate": DEFAULT_SAMPLE_RATE, "udp_dst_port": CUSTOM_UDP_DST_PORT},
                 False, id="sample_rate_and_udp_dst_port"),
]


@pytest.mark.parametrize('sampling_direction', ['rx', 'tx', 'both'], indirect=True)
@pytest.mark.parametrize("create_kwargs,requires_truncation", CREATE_CONFIG_DB_CASES)
def test_create_sflow_session_config_fields(
        request,
        duthosts,
        rand_one_dut_hostname,
        mirrored_sflow_ports,
        mirror_session_cleanup,
        sampling_direction,
        create_kwargs,
        requires_truncation):
    '''
    Verify the session type, sampling options and direction are written to the CONFIG_DB
    MIRROR_SESSION entry.
    '''
    if requires_truncation:
        request.getfixturevalue("skip_if_truncation_unsupported")

    duthost = duthosts[rand_one_dut_hostname]
    session_name = mirror_session_cleanup("test_sflow_create")
    create_mirrored_sflow_session_config(
        duthost, session_name, ERSPAN_SRC_IP, ERSPAN_DST_IP,
        source_port=mirrored_sflow_ports['source']['name'],
        direction=sampling_direction,
        **create_kwargs)

    expected = dict(create_kwargs, type="SFLOW", direction=sampling_direction.upper(),
                    src_port=mirrored_sflow_ports['source']['name'])
    assert_mirror_session_config_db_fields(duthost, session_name, expected)


def test_remove_sflow_session(
        duthosts,
        rand_one_dut_hostname,
        erspan_capabilities,
        mirrored_sflow_ports,
        mirror_session_cleanup):
    '''
    Verify removing an SFLOW session deletes its CONFIG_DB MIRROR_SESSION key.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    session_name = mirror_session_cleanup("test_sflow_remove")
    create_mirrored_sflow_session_config(
        duthost, session_name, ERSPAN_SRC_IP, ERSPAN_DST_IP,
        source_port=mirrored_sflow_ports['source']['name'],
        direction=_any_sampling_direction(erspan_capabilities), sample_rate=DEFAULT_SAMPLE_RATE)
    duthost.command('config mirror_session remove {}'.format(session_name))
    pytest_assert(not mirror_session_config_db_exists(duthost, session_name),
                  "MIRROR_SESSION|{} should not exist after removal".format(session_name))


@pytest.mark.parametrize("invalid_rate", [1, None], ids=["rate1", "missing"])
def test_invalid_sample_rate_rejected(duthosts, rand_one_dut_hostname, mirrored_sflow_ports,
                                      mirror_session_cleanup, invalid_rate):
    '''
    Verify the CLI rejects an SFLOW session with an out-of-range or missing sample_rate,
    and writes nothing to CONFIG_DB.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    session_name = mirror_session_cleanup("test_sflow_invalid_sr")
    result = create_mirrored_sflow_session_config(
        duthost, session_name, ERSPAN_SRC_IP, ERSPAN_DST_IP,
        source_port=mirrored_sflow_ports['source']['name'],
        sample_rate=invalid_rate, module_ignore_errors=True)
    pytest_assert(result['rc'] != 0,
                  "CLI should reject sample_rate={} but command succeeded".format(invalid_rate))
    pytest_assert(not mirror_session_config_db_exists(duthost, session_name),
                  "Rejected session {} should not be in CONFIG_DB".format(session_name))


@pytest.mark.parametrize("invalid_size", [32, 63, 9217])
def test_invalid_truncate_size_rejected(duthosts, rand_one_dut_hostname, mirrored_sflow_ports,
                                        mirror_session_cleanup, invalid_size):
    '''
    Verify the CLI rejects an out-of-range truncate_size (non-zero exit code).
    '''
    duthost = duthosts[rand_one_dut_hostname]
    session_name = mirror_session_cleanup("test_sflow_invalid_ts")
    result = create_mirrored_sflow_session_config(
        duthost, session_name, ERSPAN_SRC_IP, ERSPAN_DST_IP,
        source_port=mirrored_sflow_ports['source']['name'],
        sample_rate=DEFAULT_SAMPLE_RATE, truncate_size=invalid_size, module_ignore_errors=True)
    pytest_assert(result['rc'] != 0,
                  "CLI should reject truncate_size={} but command succeeded".format(invalid_size))


def test_show_mirror_session_displays_sflow(
        duthosts,
        rand_one_dut_hostname,
        erspan_capabilities,
        mirrored_sflow_ports,
        mirror_session_cleanup,
        skip_if_truncation_unsupported):
    '''
    Verify 'show mirror_session' lists the session in the SFLOW table, with its UDP
    destination port, direction, sample rate and truncate size.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    session_name = mirror_session_cleanup("test_sflow_show")
    sampling_direction = _any_sampling_direction(erspan_capabilities)
    create_mirrored_sflow_session_config(
        duthost, session_name, ERSPAN_SRC_IP, ERSPAN_DST_IP,
        source_port=mirrored_sflow_ports['source']['name'], direction=sampling_direction,
        sample_rate=512, truncate_size=DEFAULT_TRUNCATE_SIZE, udp_dst_port=CUSTOM_UDP_DST_PORT)
    output = duthost.shell("show mirror_session")['stdout']
    pytest_assert("SFLOW Sessions" in output,
                  "show mirror_session has no SFLOW table:\n{}".format(output))
    sflow_table = output.split("SFLOW Sessions", 1)[1]
    rows = [ln for ln in sflow_table.splitlines() if ln.split() and ln.split()[0] == session_name]
    pytest_assert(len(rows) == 1,
                  "Expected one '{}' row in the SFLOW table, got:\n{}".format(session_name, output))
    fields = rows[0].split()
    for value in ('512', str(DEFAULT_TRUNCATE_SIZE), str(CUSTOM_UDP_DST_PORT), sampling_direction):
        pytest_assert(value in fields, "SFLOW row should list {}, row: {!r}".format(value, rows[0]))


# ---------------------------------------------------------------------------
# Sampling and datagram contents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('mirrored_sflow_session',
                         [{'sample_rate': 100}, {'sample_rate': 256}],
                         ids=['rate100', 'rate256'], indirect=True)
def test_sflow_sampling_rx(
        skip_if_vs_platform,
        skip_if_ingress_sampling_unsupported,
        ptfhost,
        ptfadapter,
        mirrored_sflow_session):
    '''
    Verify RX sampling yields 1 sample per N probes (SFLOW_NUM_SAMPLES +-10%), each reporting
    the configured rate and the source port as input ifIndex.
    '''
    samples, _, _ = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session,
                                     mirrored_sflow_session['source_index'])
    assert_sample_count(len(samples), SFLOW_NUM_SAMPLES)
    assert_sample_fields(samples, sample_rate=mirrored_sflow_session['sample_rate'],
                         input_port=mirrored_sflow_session['source_ifindex'])


@pytest.mark.parametrize('mirrored_sflow_session',
                         [{'sample_rate': 100, 'direction': 'tx'},
                          {'sample_rate': 256, 'direction': 'tx'}],
                         ids=['rate100', 'rate256'], indirect=True)
def test_sflow_sampling_tx(
        skip_if_vs_platform,
        skip_if_egress_sampling_unsupported,
        skip_if_no_mirrored_sflow_tx_ingress,
        ptfhost,
        ptfadapter,
        mirrored_sflow_session):
    '''
    Verify TX sampling: probes injected on another port are routed out the source port
    and sampled there at 1:N, each sample reporting the source port as output ifIndex.
    '''
    inject, probe = _inject(mirrored_sflow_session, 'tx')
    samples, _, _ = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session, inject, probe=probe)
    assert_sample_count(len(samples), SFLOW_NUM_SAMPLES)
    assert_sample_fields(samples, sample_rate=mirrored_sflow_session['sample_rate'],
                         output_port=mirrored_sflow_session['source_ifindex'])


@pytest.mark.parametrize('mirrored_sflow_session',
                         [{'sample_rate': 100, 'direction': 'both'},
                          {'sample_rate': 256, 'direction': 'both'}],
                         ids=['rate100', 'rate256'], indirect=True)
def test_sflow_sampling_both(
        skip_if_vs_platform,
        skip_if_ingress_sampling_unsupported,
        skip_if_egress_sampling_unsupported,
        skip_if_no_mirrored_sflow_tx_ingress,
        ptfhost,
        ptfadapter,
        mirrored_sflow_session):
    '''
    Verify direction=both samples the RX leg and the TX leg, each at 1:N.
    '''
    for leg, port_field in (('rx', 'input_port'), ('tx', 'output_port')):
        inject, probe = _inject(mirrored_sflow_session, leg)
        samples, _, _ = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session, inject, probe=probe)
        assert_sample_count(len(samples), SFLOW_NUM_SAMPLES, label="BOTH {} leg".format(leg.upper()))
        assert_sample_fields(samples, **{port_field: mirrored_sflow_session['source_ifindex']})


@pytest.mark.parametrize('mirrored_sflow_session',
                         [{'sample_rate': TRUNCATION_SAMPLE_RATE, 'truncate_size': DEFAULT_TRUNCATE_SIZE,
                           'direction': direction} for direction in ('rx', 'tx')],
                         ids=['rx', 'tx'], indirect=True)
@pytest.mark.parametrize('pktlen', [64, 1500, DEFAULT_TRUNCATE_SIZE])
def test_sflow_truncation(
        request,
        skip_if_vs_platform,
        skip_if_truncation_unsupported,
        ptfhost,
        ptfadapter,
        mirrored_sflow_session,
        pktlen):
    '''
    Verify every sampled header is min(frame, truncate_size) bytes long and is exactly
    the probe's leading bytes, with nothing the switch added internally.
    '''
    if mirrored_sflow_session['direction'] == 'tx':
        request.getfixturevalue('skip_if_no_mirrored_sflow_tx_ingress')
    truncate_size = mirrored_sflow_session['truncate_size']
    inject, probe = _inject(mirrored_sflow_session, mirrored_sflow_session['direction'])
    samples, _, probe_bytes = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session, inject, probe=probe,
                                               count=TRUNCATION_PROBE_COUNT, pktlen=pktlen)
    pytest_assert(samples, "No samples for truncate_size={} pktlen={}".format(truncate_size, pktlen))
    for sample in samples:
        header = header_bytes(sample)
        if pktlen >= truncate_size:
            pytest_assert(len(header) == truncate_size,
                          "Header length {} != truncate_size {} (pktlen {})".format(
                              len(header), truncate_size, pktlen))
        else:
            pytest_assert(pktlen <= len(header) <= truncate_size,
                          "Header length {} should hold the whole {}B frame and stay within {}".format(
                              len(header), pktlen, truncate_size))
        compared = min(len(header), len(probe_bytes))
        pytest_assert(header[:compared] == probe_bytes[:compared],
                      "Sampled header differs from the probe's first {} bytes".format(compared))


@pytest.mark.parametrize('mirrored_sflow_session',
                         [{}, {'udp_dst_port': CUSTOM_UDP_DST_PORT}],
                         ids=['default_port', 'custom_port'], indirect=True)
def test_sflow_datagram_contents(
        skip_if_vs_platform,
        skip_if_ingress_sampling_unsupported,
        ptfhost,
        ptfadapter,
        mirrored_sflow_session):
    '''
    Verify the datagram fields: version 5 header with gapless datagram sequence numbers,
    agent = session src_ip, UDP source port 6343 and the configured destination port, the
    configured sampling rate, the source port as source_id and input ifIndex, no drops,
    an Ethernet header holding the probe's bytes, and the probe's frame length.
    '''
    pktlen = 300
    samples, capture, probe_bytes = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session,
                                                     mirrored_sflow_session['source_index'],
                                                     count=100 * mirrored_sflow_session['sample_rate'],
                                                     pktlen=pktlen)
    pytest_assert(samples, "No samples on UDP port {}".format(mirrored_sflow_session['udp_dst_port']))
    pytest_assert(capture.src_port_ok, "Some datagrams did not come from UDP source port 6343")
    assert_datagram_headers(capture.datagrams, mirrored_sflow_session['src_ip'])
    assert_sample_fields(samples, sample_rate=mirrored_sflow_session['sample_rate'],
                         input_port=mirrored_sflow_session['source_ifindex'])
    assert_flow_sample_source_fields(samples, mirrored_sflow_session['source_ifindex'])
    for sample in samples:
        pytest_assert(sample['agent'] == mirrored_sflow_session['src_ip'],
                      "Agent {} != session src_ip {}".format(sample['agent'], mirrored_sflow_session['src_ip']))
        # frame_length may or may not count the 4-byte FCS
        frame_len = int(sample['header']['sampledPacketSize']) - int(sample['header'].get('strippedBytes', 0))
        pytest_assert(frame_len in (pktlen, pktlen + 4),
                      "Frame length {} does not match the {}B probe".format(frame_len, pktlen))
        header = header_bytes(sample)
        compared = min(len(header), len(probe_bytes))
        pytest_assert(header[:compared] == probe_bytes[:compared],
                      "Sampled header differs from the probe's first {} bytes".format(compared))


@pytest.mark.parametrize('direction', ['rx', 'tx'])
def test_sflow_portchannel_source(
        request,
        skip_if_vs_platform,
        duthosts,
        rand_one_dut_hostname,
        ptfhost,
        ptfadapter,
        mirror_session_cleanup,
        mirrored_sflow_ports,
        setup_mirrored_sflow_route,
        direction):
    '''
    Verify a session on a PortChannel samples its member ports.

    RX: probes injected on each member are sampled at 1:N, and each sample reports that
    member as input ifIndex.
    TX: probes routed to the PortChannel peer leave through one member; they are sampled
    at 1:N with that member as output ifIndex.
    '''
    request.getfixturevalue('skip_if_{}_sampling_unsupported'.format('ingress' if direction == 'rx' else 'egress'))
    duthost = duthosts[rand_one_dut_hostname]
    pc_name, pc = _pick_portchannel(mirrored_sflow_ports)
    rate = DEFAULT_SAMPLE_RATE
    _create_active_session(duthost, mirror_session_cleanup, "test_sflow_pc", pc_name, rate, direction)
    session = mirrored_sflow_view(mirrored_sflow_ports, sample_rate=rate)
    member_ifindexes = [mirrored_sflow_ports['ifindex'][m['name']] for m in pc['members']]

    if direction == 'rx':
        for member in pc['members']:
            probe = make_probe(session['router_mac'], ip_dst=mirrored_sflow_ports['route_dst_ip'])
            samples, _, _ = _capture_samples(ptfhost, ptfadapter, session, member['index'], probe=probe)
            assert_sample_count(len(samples), SFLOW_NUM_SAMPLES, label=member['name'])
            assert_sample_fields(samples, input_port=mirrored_sflow_ports['ifindex'][member['name']])
    else:
        probe = make_probe(session['router_mac'], ip_dst=pc['peer_addr'])
        samples, _, _ = _capture_samples(ptfhost, ptfadapter, session, session['source_index'], probe=probe)
        assert_sample_count(len(samples), SFLOW_NUM_SAMPLES)
        for sample in samples:
            pytest_assert(int(sample['outputPort']) in member_ifindexes,
                          "Output ifIndex {} is not a {} member {}".format(
                              sample['outputPort'], pc_name, member_ifindexes))


@pytest.mark.parametrize('mirrored_sflow_session',
                         [{'direction': 'rx'}, {'direction': 'tx'}],
                         ids=['rx', 'tx'], indirect=True)
def test_sflow_session_remove_stops_sampling(
        request,
        skip_if_vs_platform,
        duthosts,
        rand_one_dut_hostname,
        ptfhost,
        ptfadapter,
        mirrored_sflow_session):
    '''
    Verify removing the session stops sampling: probes sent afterwards give no samples.
    '''
    if mirrored_sflow_session['direction'] == 'tx':
        request.getfixturevalue('skip_if_no_mirrored_sflow_tx_ingress')
    duthost = duthosts[rand_one_dut_hostname]
    duthost.command('config mirror_session remove {}'.format(mirrored_sflow_session['session_name']))
    inject, probe = _inject(mirrored_sflow_session, mirrored_sflow_session['direction'])
    samples, _, _ = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session, inject, probe=probe,
                                     count=100 * mirrored_sflow_session['sample_rate'])
    pytest_assert(not samples, "Got {} samples after session removal".format(len(samples)))


@pytest.mark.parametrize('mirrored_sflow_session', [{'sample_rate': 256}], ids=['rate256'], indirect=True)
def test_sflow_session_add_after_remove(
        skip_if_vs_platform,
        skip_if_ingress_sampling_unsupported,
        duthosts,
        rand_one_dut_hostname,
        ptfhost,
        ptfadapter,
        mirror_session_cleanup,
        mirrored_sflow_session):
    '''
    Verify a session removed and added again with a new rate samples at the new rate.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    new_rate = 100
    duthost.command('config mirror_session remove {}'.format(mirrored_sflow_session['session_name']))
    _create_active_session(duthost, mirror_session_cleanup, mirrored_sflow_session['session_name'],
                           mirrored_sflow_session['source_port'], new_rate)
    session = _session_view(mirrored_sflow_session, sample_rate=new_rate)
    samples, _, _ = _capture_samples(ptfhost, ptfadapter, session, session['source_index'])
    assert_sample_count(len(samples), SFLOW_NUM_SAMPLES)
    assert_sample_fields(samples, sample_rate=new_rate, input_port=session['source_ifindex'])


EVERFLOW_TABLE = "SFLOW_EVERFLOW"
EVERFLOW_RULE = "SFLOW_PROBE"


@pytest.fixture
def acl_erspan(duthosts, rand_one_dut_hostname, mirror_session_cleanup):
    '''
    Give the test a function that mirrors one source IP arriving on a port to a new
    ERSPAN session through a MIRROR ACL table. The CLI allows one port mirror session per
    port and direction, so an ACL is the way to mirror a port that already has an SFLOW
    session.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    added = {}

    def existing_mirror_table(port):
        # orchagent allows one ingress MIRROR table, and minigraphs usually create EVERFLOW
        tables = duthost.shell("sonic-db-cli CONFIG_DB KEYS 'ACL_TABLE|*'")['stdout'].split()
        for key in tables:
            table = _config_db_hgetall(duthost, key)
            if (table.get('type') == 'MIRROR' and table.get('stage', 'ingress').lower() == 'ingress'
                    and port in table.get('ports@', '').split(',')):
                return key.split('|', 1)[1]
        return None

    def add(port, src_ip, session_name):
        create_erspan_session_config(duthost, mirror_session_cleanup(session_name))
        table = existing_mirror_table(port)
        if table is None:
            table = EVERFLOW_TABLE
            duthost.command("config acl add table {} MIRROR -p {} -s ingress".format(table, port))
            added['table'] = table
        added['rule'] = "ACL_RULE|{}|{}".format(table, EVERFLOW_RULE)
        duthost.shell("sonic-db-cli CONFIG_DB HSET '{}' PRIORITY 9999 SRC_IP {}/32 MIRROR_INGRESS_ACTION {}".format(
            added['rule'], src_ip, session_name))
        state_cmd = "sonic-db-cli STATE_DB HGET 'ACL_RULE_TABLE|{}|{}' status".format(table, EVERFLOW_RULE)
        pytest_assert(wait_until(30, 2, 0, lambda: duthost.shell(state_cmd)['stdout'].strip() == 'Active'),
                      "ACL rule {} did not go Active".format(added['rule']))

    yield add

    if 'rule' in added:
        duthost.shell("sonic-db-cli CONFIG_DB DEL '{}'".format(added['rule']), module_ignore_errors=True)
    if 'table' in added:
        duthost.command("config acl remove table {}".format(added['table']), module_ignore_errors=True)


@pytest.mark.parametrize('mirrored_sflow_session', [{'sample_rate': 4}], ids=['rate4'], indirect=True)
def test_sflow_with_erspan_same_port(
        skip_if_vs_platform,
        skip_if_ingress_sampling_unsupported,
        duthosts,
        rand_one_dut_hostname,
        ptfhost,
        ptfadapter,
        acl_erspan,
        mirrored_sflow_session):
    '''
    Verify ACL-based ERSPAN and an SFLOW session work together on the same port: ERSPAN
    mirrors every probe, and sFlow samples 1 in 4.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    erspan_name = "test_sflow_erspan_acl"
    acl_erspan(mirrored_sflow_session['source_port'], mirrored_sflow_session['probe']['ip_src'], erspan_name)
    pytest_assert(wait_mirror_session_active(duthost, erspan_name),
                  "ERSPAN session alongside SFLOW did not go active")
    pytest_assert(get_mirror_session_status(duthost, mirrored_sflow_session['session_name']) == 'active',
                  "SFLOW session went inactive after adding ERSPAN on the same port")

    probe_count = 4000
    samples, _, _ = _capture_samples(ptfhost, ptfadapter, mirrored_sflow_session,
                                     mirrored_sflow_session['source_index'],
                                     count=probe_count)
    # The ASIC picks the GRE protocol of ACL-mirrored ERSPAN, not the session config
    gre_type = BaseEverflowTest.mirror_session_info(erspan_name, duthost.facts['asic_type'])['session_gre']
    erspan_info = {'src_ip': ERSPAN_SRC_IP, 'dst_ip': ERSPAN_DST_IP, 'gre_type': hex(gre_type)}
    mirrored = collect_erspan_packets(ptfadapter, mirrored_sflow_session['collector_indices'], erspan_info, timeout=5)
    pytest_assert(len(mirrored) == probe_count,
                  "ERSPAN mirrored {} of {} probes".format(len(mirrored), probe_count))
    assert_sample_count(len(samples), probe_count // mirrored_sflow_session['sample_rate'], tolerance=0.2)


def _config_db_hgetall(duthost, key):
    output = duthost.shell("sonic-db-cli CONFIG_DB HGETALL '{}'".format(key))['stdout'].strip()
    return ast.literal_eval(output) if output else {}


def _config_db_restore(duthost, key, fields):
    '''Put a CONFIG_DB key back to `fields`, deleting it if `fields` is empty.'''
    duthost.shell("sonic-db-cli CONFIG_DB DEL '{}'".format(key), module_ignore_errors=True)
    if fields:
        pairs = ' '.join('{} {}'.format(k, v) for k, v in fields.items())
        duthost.shell("sonic-db-cli CONFIG_DB HSET '{}' {}".format(key, pairs))


@pytest.fixture
def sw_sflow(duthosts, rand_one_dut_hostname):
    '''
    Give the test a function that enables software sFlow (hsflowd) on a port, and restore
    the SFLOW and SFLOW_SESSION rows afterwards.

    Software sFlow needs the sflow feature enabled, so the fixture enables it and puts it back.

    Teardown disables through the CLI first: sflowmgrd ignores deleted rows, so deleting
    them alone leaves the port's samplepacket bound. The disabled rows are then put back
    as they were, since the post-test config check flags leftover rows.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    feature_state = duthost.shell("sonic-db-cli CONFIG_DB HGET 'FEATURE|sflow' state")['stdout'].strip()
    pytest_require(feature_state, "sflow feature is not available on this image")
    if feature_state != 'enabled':
        duthost.command("config feature state sflow enabled")
    saved = {}
    ports = []

    def enable(port, rate=DEFAULT_SAMPLE_RATE):
        ports.append(port)
        for key in ('SFLOW|global', 'SFLOW_SESSION|{}'.format(port)):
            saved.setdefault(key, _config_db_hgetall(duthost, key))
        duthost.command("config sflow enable")
        duthost.command("config sflow interface enable {}".format(port))
        duthost.command("config sflow interface sample-rate {} {}".format(port, rate))

    yield enable

    for port in ports:
        duthost.command("config sflow interface disable {}".format(port), module_ignore_errors=True)
    duthost.command("config sflow disable", module_ignore_errors=True)

    def unbound():
        keys = duthost.shell("sonic-db-cli APPL_DB KEYS 'SFLOW_SESSION_TABLE:*'")['stdout'].split()
        return not any(key.split(':', 1)[1] in ports for key in keys)
    is_unbound = wait_until(30, 2, 0, unbound)

    for key, fields in saved.items():
        _config_db_restore(duthost, key, fields)
    if feature_state != 'enabled':
        duthost.command("config feature state sflow {}".format(feature_state))
    pytest_assert(is_unbound, "sFlow still applied to {} after disable".format(ports))


@pytest.mark.parametrize('order', ['sw_first', 'hw_first'])
def test_sflow_with_sw_sflow_same_port(
        skip_if_vs_platform,
        skip_if_ingress_sampling_unsupported,
        duthosts,
        rand_one_dut_hostname,
        ptfhost,
        ptfadapter,
        mirrored_sflow_ports,
        setup_mirrored_sflow_route,
        mirror_session_cleanup,
        sw_sflow,
        loganalyzer,
        order):
    '''
    Verify software sFlow and an SFLOW session cannot both sample one port.

    sw_first: with software sFlow on the port, an SFLOW session there does not go active.
    hw_first: with an active SFLOW session, enabling software sFlow on the port leaves the
    session active and sampling at its own rate.
    '''
    duthost = duthosts[rand_one_dut_hostname]
    port = mirrored_sflow_ports['source']['name']
    name = "test_sflow_vs_hsflowd"
    if loganalyzer:
        # orchagent logs each refused bind at ERR
        loganalyzer[duthost.hostname].ignore_regex.extend([
            r".*ERR swss#orchagent:.*cannot bind sFlow.*",
            r".*ERR swss#orchagent:.*cannot bind sampled mirror.*",
            r".*ERR swss#orchagent:.*configurePortMirrorSession: Failed to configure mirror session {}.*".format(name),
            r".*ERR swss#orchagent:.*activateSession: Failed to activate port mirror session {}.*".format(name),
        ])

    if order == 'sw_first':
        sw_sflow(port)
        _create_active_session(duthost, mirror_session_cleanup, name, port, DEFAULT_SAMPLE_RATE,
                               expect_active=False)
        return

    _create_active_session(duthost, mirror_session_cleanup, name, port, DEFAULT_SAMPLE_RATE)
    sw_sflow(port, rate=1000)
    pytest_assert(wait_mirror_session_active(duthost, name),
                  "SFLOW session went inactive after software sFlow was enabled on {}".format(port))
    session = mirrored_sflow_view(mirrored_sflow_ports, sample_rate=DEFAULT_SAMPLE_RATE)
    samples, _, _ = _capture_samples(ptfhost, ptfadapter, session, session['source_index'])
    assert_sample_count(len(samples), SFLOW_NUM_SAMPLES)
    assert_sample_fields(samples, sample_rate=DEFAULT_SAMPLE_RATE)
