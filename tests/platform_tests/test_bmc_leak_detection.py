"""
End-to-end liquid-cooling leak detection on a SONiC BMC chassis.

The thermalctld/bmcctld daemon tests inject leak state straight into STATE_DB. These tests
drive the leak sensing hardware instead (through the vendor LiquidLeakageMockerBMC) and check
the whole chain: sensor -> BMC leak-fault input -> thermalctld -> STATE_DB and CLI -> bmcctld
policy action -> Switch-Host power, plus the recovery path once the leak clears.

Covered (see "Leak Detection End-to-End Tests" in the BMC platform API and daemon test plan):
- single-sensor MINOR detection, logging and recovery with the Switch-Host untouched
- CRITICAL leak powers the Switch-Host off, power-on stays blocked while the leak is present,
  the host comes back once the leak is cleared
- MINOR -> CRITICAL (MAJOR on images with MAJOR support) escalation after max_minor_duration_sec
- leak-action configuration: syslog_only and a disabled policy leave the Switch-Host up
- Switch-Host reboot-cause after a BMC leak power-off
"""

import logging
import re
import time

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.liquid_leakage_control_test_helper import get_liquid_cooling_update_interval
from tests.common.helpers.sensor_control_test_helper import mocker_factory  # noqa: F401
from tests.common.platform.bmc_utils import (
    BMC_EVENT_LOG,
    CAUSE_POWER_DOWN_FROM_BMC,
    get_host_boot_id,
    get_host_uptime,
    get_leak_control_policy,
    get_switch_host_oper_status,
    get_switch_host_or_skip_test,
    get_system_leak_status,
    is_major_leak_supported,
    leak_control_policy_restored,
    make_bmc_loganalyzer,
    set_system_leak_action,
    set_system_leak_policy,
    verify_bmc_initiated_reboot,
    wait_host_off,
    wait_system_leak_status,
)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('bmc'),
    pytest.mark.disable_loganalyzer,
]

DEFAULT_POLL_SEC = 0.5
DETECTION_TIMEOUT_SEC = 10
# bmcctld powers the host off within a second of the CRITICAL status; the budget covers the
# BMC-side module-status and ping polling that confirms it.
POWER_OFF_TIMEOUT_SEC = 120
# How long the Switch-Host must stay up before a non-power action counts as "no action".
HOST_UP_SETTLE_SEC = 20
# Escalation must not fire this long before max_minor_duration_sec, and must have fired this
# long after it (thermalctld polls every 0.5 s; the rest absorbs CLI latency).
ESCALATION_TOLERANCE_SEC = 10
MAX_ESCALATION_WAIT_SEC = 900

# Pre-power-off state of the first CRITICAL cycle in this module, so the reboot-cause test does
# not need a second multi-minute power cycle when it runs after the power-off test.
_last_leak_power_off = {}


@pytest.fixture
def bmc(duthosts, enum_rand_one_per_hwsku_hostname):
    """The BMC under test; skips unless it is a liquid-cooled SONiC BMC with both leak daemons running."""
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    if not duthost.is_bmc():
        pytest.skip("Device is not a BMC system")
    out = duthost.shell(
        "python3 -c 'from sonic_platform.chassis import Chassis; print(Chassis().is_liquid_cooled())'",
        module_ignore_errors=True)
    if (out.get('stdout') or '').strip() != 'True':
        pytest.skip("Chassis is not liquid-cooled")
    for daemon in ('thermalctld', 'bmcctld'):
        status, _ = duthost.get_pmon_daemon_status(daemon)
        pytest_assert(status == 'RUNNING', "{} must be RUNNING for the leak detection tests".format(daemon))
    return duthost


@pytest.fixture
def switch_host(bmc):
    return get_switch_host_or_skip_test(bmc)


@pytest.fixture
def leak_mocker(bmc, mocker_factory):  # noqa: F811
    status = get_system_leak_status(bmc)
    if status in ('MINOR', 'MAJOR', 'CRITICAL'):
        pytest.skip("System already reports a {} leak; not injecting on top of it".format(status))
    leak_mocker = mocker_factory(bmc, 'LiquidLeakageMockerBMC')
    if leak_mocker is None:
        pytest.skip("No LiquidLeakageMockerBMC available on this platform")
    return leak_mocker


@pytest.fixture
def leak_policy(bmc):
    """LEAK_CONTROL_POLICY|policy snapshot; whatever a test configures is restored afterwards."""
    with leak_control_policy_restored(bmc) as snapshot:
        yield snapshot


def _sensor_for(leak_mocker, severity):
    """Index of a sensor that can report `severity`; vendor mockers may fix the severity per sensor."""
    supporting = getattr(leak_mocker, 'sensors_supporting', None)
    candidates = supporting(severity) if supporting else list(range(len(leak_mocker.sensor_names)))
    if not candidates:
        pytest.skip("No leak sensor on this platform can report a {} leak".format(severity))
    return candidates[0]


def _poll_sec(bmc):
    return get_liquid_cooling_update_interval(bmc) or DEFAULT_POLL_SEC


def _matched_lines(la, marker, regexes, log_target):
    la.match_regex = list(regexes)
    result = la.analyze(marker, fail=False, log_target=log_target)
    lines = []
    for matched in (result.get('match_messages') or {}).values():
        lines.extend(matched)
    return lines


def _assert_logged(lines, regex, what):
    pytest_assert(any(re.search(regex, line) for line in lines),
                  "{}: no log line matched {!r}; candidates: {}".format(what, regex, lines[:5]))


def _leak_cli_row(bmc, sensor_name):
    for row in bmc.show_and_parse("show platform leak status") or []:
        if (row.get('name') or '').strip() == sensor_name:
            return row
    pytest.fail("{} missing from 'show platform leak status'".format(sensor_name))


def _assert_host_stayed_up(switch_host, pre_uptime, context):
    time.sleep(HOST_UP_SETTLE_SEC)
    post_uptime = get_host_uptime(switch_host)
    pytest_assert(post_uptime == pre_uptime,
                  "Switch-Host rebooted or powered off {} (uptime {!r} -> {!r})".format(
                      context, pre_uptime, post_uptime))


def _wait_cleared(bmc, leak_mocker, index):
    name = leak_mocker.get_sensor_name(index)
    pytest_assert(wait_system_leak_status(bmc, 'None', DETECTION_TIMEOUT_SEC, _poll_sec(bmc)),
                  "SYSTEM_LEAK_STATUS still {!r} after clearing the leak on {}".format(
                      get_system_leak_status(bmc), name))
    leaking, _, _ = leak_mocker.get_sensor_status(index)
    pytest_assert(not leaking, "{} still reports leaking after the leak was cleared".format(name))
    pytest_assert(_leak_cli_row(bmc, name).get('leak') == 'No',
                  "'show platform leak status' still shows {} leaking".format(name))


def test_minor_leak_single_sensor(bmc, switch_host, leak_mocker):
    """A single MINOR leak is detected, shown, logged (syslog only) and clears; the Switch-Host stays up."""
    index = _sensor_for(leak_mocker, 'MINOR')
    name = leak_mocker.get_sensor_name(index)
    pre_uptime = get_host_uptime(switch_host)
    la = make_bmc_loganalyzer(bmc, "leak_e2e_minor_single_sensor")
    marker = la.init(log_target='syslog')
    ev_marker = la.init()
    try:
        started = time.monotonic()
        leak_mocker.set_sensor_state(index, 'MINOR')
        pytest_assert(wait_system_leak_status(bmc, 'MINOR', DETECTION_TIMEOUT_SEC, _poll_sec(bmc)),
                      "SYSTEM_LEAK_STATUS did not become MINOR, got {!r}".format(get_system_leak_status(bmc)))
        logger.info("SYSTEM_LEAK_STATUS=MINOR %.1fs after injecting %s", time.monotonic() - started, name)

        leaking, severity, sensor_status = leak_mocker.get_sensor_status(index)
        pytest_assert(leaking and severity == 'MINOR' and sensor_status == 'Good',
                      "{}: expected leaking/MINOR/Good in STATE_DB, got {}/{}/{}".format(
                          name, leaking, severity, sensor_status))
        row = _leak_cli_row(bmc, name)
        pytest_assert(row.get('leak') == 'Yes' and row.get('leak-severity') == 'MINOR',
                      "'show platform leak status' row for {}: {}".format(name, row))

        _assert_host_stayed_up(switch_host, pre_uptime, "on a MINOR leak (default action syslog_only)")

        lines = _matched_lines(la, marker, [r".*reported leaking.*", r".*MINOR_SYSTEM_LEAK.*"], 'syslog')
        _assert_logged(lines, r"Liquid cooling leakage sensor {} reported leaking".format(re.escape(name)),
                       "thermalctld")
        _assert_logged(lines, r"MINOR_SYSTEM_LEAK", "bmcctld MINOR event")
        minor_in_event_log = [line for line in _matched_lines(la, ev_marker, [r"(?i).*leak.*"], 'event_log')
                              if 'loganalyzer' not in line.lower()]
        pytest_assert(not minor_in_event_log,
                      "A MINOR leak must stay out of {}: {}".format(BMC_EVENT_LOG, minor_in_event_log[:3]))
    finally:
        recovery_marker = la.init(log_target='syslog')
        leak_mocker.set_sensor_state(index, 'None')

    _wait_cleared(bmc, leak_mocker, index)
    _assert_logged(_matched_lines(la, recovery_marker, [r".*recovered from leaking.*"], 'syslog'),
                   r"Liquid cooling leakage sensor {} recovered from leaking".format(re.escape(name)),
                   "thermalctld recovery")


def _critical_leak_power_cycle(bmc, switch_host, leak_mocker, la):
    """Inject a CRITICAL leak, verify the power-off and the blocked power-on, recover. Returns the pre-state."""
    index = _sensor_for(leak_mocker, 'CRITICAL')
    name = leak_mocker.get_sensor_name(index)
    pre = {'uptime': get_host_uptime(switch_host), 'boot_id': get_host_boot_id(switch_host)}
    pytest_assert(pre['uptime'] and pre['boot_id'], "Unable to read the Switch-Host uptime/boot id before the leak")
    ev_marker = la.init()
    try:
        started = time.monotonic()
        leak_mocker.set_sensor_state(index, 'CRITICAL')
        pytest_assert(wait_system_leak_status(bmc, 'CRITICAL', DETECTION_TIMEOUT_SEC, _poll_sec(bmc)),
                      "SYSTEM_LEAK_STATUS did not become CRITICAL, got {!r}".format(get_system_leak_status(bmc)))
        detected_after = time.monotonic() - started
        pytest_assert(wait_host_off(bmc, switch_host, timeout=POWER_OFF_TIMEOUT_SEC, interval=5, delay=2),
                      "Switch-Host did not power off after the CRITICAL leak on {}".format(name))
        logger.info("CRITICAL leak on %s: STATE_DB after %.1fs, Switch-Host off (BMC view) after %.1fs",
                    name, detected_after, time.monotonic() - started)
        row = _leak_cli_row(bmc, name)
        pytest_assert(row.get('leak') == 'Yes' and row.get('leak-severity') == 'CRITICAL',
                      "'show platform leak status' row for {}: {}".format(name, row))
        ev_lines = _matched_lines(la, ev_marker, [r".*CRITICAL system leak detected.*", r".*ACTION DONE.*"],
                                  'event_log')
        _assert_logged(ev_lines, r"CRITICAL system leak detected", "thermalctld event log")
        _assert_logged(ev_lines, r"ACTION DONE: action='power_off' trigger='CRITICAL_SYSTEM_LEAK'",
                       "bmcctld event log")

        # While the leak is present the BMC must refuse to power the host back on.
        blocked_marker = la.init(log_target='syslog')
        bmc.shell("config chassis modules shutdown SWITCH-HOST", module_ignore_errors=True)
        bmc.shell("config chassis modules startup SWITCH-HOST", module_ignore_errors=True)
        time.sleep(HOST_UP_SETTLE_SEC)
        oper_status = get_switch_host_oper_status(bmc)
        pytest_assert(oper_status == 'offline',
                      "Switch-Host came back (oper-status {!r}) while the CRITICAL leak was still present".format(
                          oper_status))
        _assert_logged(_matched_lines(la, blocked_marker, [r".*BLOCKED.*"], 'syslog'),
                       r"admin_up.*BLOCKED \(CRITICAL_LEAK_PRESENT", "bmcctld power-on gate")
    finally:
        # The mocker disables the leak policy, powers the host back on, clears the injection and
        # restores the policy; thermalctld then clears the system status.
        leak_mocker.set_sensor_state(index, 'None')

    _wait_cleared(bmc, leak_mocker, index)
    _assert_logged(_matched_lines(la, ev_marker, [r".*CRITICAL system leak cleared.*"], 'event_log'),
                   r"CRITICAL system leak cleared", "thermalctld event log")
    post_boot_id = get_host_boot_id(switch_host)
    pytest_assert(post_boot_id and post_boot_id != pre['boot_id'],
                  "Switch-Host boot id did not change: the leak power-off did not really cut power")
    oper_status = get_switch_host_oper_status(bmc)
    pytest_assert(oper_status == 'online',
                  "Switch-Host oper-status {!r} after recovery, expected online".format(oper_status))
    _last_leak_power_off.update(pre)
    return pre


def test_critical_leak_powers_off_switch_host(bmc, switch_host, leak_mocker, leak_policy):
    """A CRITICAL leak powers the Switch-Host off, keeps it off while present, and the host recovers."""
    set_system_leak_policy(bmc, True)
    set_system_leak_action(bmc, 'critical', 'power_off')
    la = make_bmc_loganalyzer(bmc, "leak_e2e_critical_power_off")
    _critical_leak_power_cycle(bmc, switch_host, leak_mocker, la)


def test_reboot_cause_after_leak_power_off(bmc, switch_host, leak_mocker, leak_policy):
    """After a BMC leak power-off the Switch-Host attributes its reboot to the BMC."""
    if _last_leak_power_off:
        pre = dict(_last_leak_power_off)
    else:
        set_system_leak_policy(bmc, True)
        set_system_leak_action(bmc, 'critical', 'power_off')
        la = make_bmc_loganalyzer(bmc, "leak_e2e_reboot_cause")
        pre = _critical_leak_power_cycle(bmc, switch_host, leak_mocker, la)
    verify_bmc_initiated_reboot(switch_host, pre['uptime'], CAUSE_POWER_DOWN_FROM_BMC)


def test_minor_leak_escalates_after_max_minor_duration(bmc, switch_host, leak_mocker, leak_policy):
    """A MINOR leak left in place escalates once max_minor_duration_sec elapses, not before."""
    index = _sensor_for(leak_mocker, 'MINOR')
    name = leak_mocker.get_sensor_name(index)
    duration = leak_mocker.sensors[name].get('max_minor_duration_sec')
    if not duration:
        pytest.skip("{} has no MINOR escalation window configured".format(name))
    if duration > MAX_ESCALATION_WAIT_SEC:
        pytest.skip("{} escalates after {}s, longer than this test waits".format(name, duration))
    escalated = 'MAJOR' if is_major_leak_supported(bmc) else 'CRITICAL'
    set_system_leak_policy(bmc, True)
    if escalated == 'CRITICAL':
        set_system_leak_action(bmc, 'critical', 'power_off')
    pre_uptime = get_host_uptime(switch_host)
    poll = _poll_sec(bmc)
    la = make_bmc_loganalyzer(bmc, "leak_e2e_minor_escalation")
    marker = la.init(log_target='syslog')
    ev_marker = la.init()
    try:
        started = time.monotonic()
        leak_mocker.set_sensor_state(index, 'MINOR')
        pytest_assert(wait_system_leak_status(bmc, 'MINOR', DETECTION_TIMEOUT_SEC, poll),
                      "SYSTEM_LEAK_STATUS did not become MINOR, got {!r}".format(get_system_leak_status(bmc)))

        early_check_at = started + duration - ESCALATION_TOLERANCE_SEC
        time.sleep(max(0, early_check_at - time.monotonic()))
        status = get_system_leak_status(bmc)
        pytest_assert(status == 'MINOR',
                      "Leak on {} escalated to {} {:.0f}s early (max_minor_duration_sec={}s)".format(
                          name, status, duration - (time.monotonic() - started), duration))
        pytest_assert(wait_system_leak_status(bmc, escalated, 2 * ESCALATION_TOLERANCE_SEC, poll),
                      "Leak on {} did not escalate to {} within {}s of max_minor_duration_sec={}s, status {!r}".format(
                          name, escalated, ESCALATION_TOLERANCE_SEC, duration, get_system_leak_status(bmc)))
        logger.info("MINOR leak on %s escalated to %s after %.1fs (max_minor_duration_sec=%ds)",
                    name, escalated, time.monotonic() - started, duration)
        _assert_logged(_matched_lines(la, marker, [r".*escalated from MINOR.*"], 'syslog'),
                       r"Leak on sensor {} escalated from MINOR to {} after \d+s".format(re.escape(name), escalated),
                       "thermalctld escalation")

        if escalated == 'CRITICAL':
            pytest_assert(wait_host_off(bmc, switch_host, timeout=POWER_OFF_TIMEOUT_SEC, interval=5, delay=2),
                          "Switch-Host did not power off after the MINOR leak escalated to CRITICAL")
            _assert_logged(_matched_lines(la, ev_marker, [r".*ACTION DONE.*"], 'event_log'),
                           r"ACTION DONE: action='power_off' trigger='CRITICAL_SYSTEM_LEAK'", "bmcctld event log")
        else:
            _assert_host_stayed_up(switch_host, pre_uptime, "on a MAJOR leak (default action syslog_only)")
    finally:
        leak_mocker.set_sensor_state(index, 'None')
    _wait_cleared(bmc, leak_mocker, index)


@pytest.mark.parametrize('policy_enabled, critical_action, expected_log', [
    pytest.param(True, 'syslog_only',
                 r"Event 'CRITICAL_SYSTEM_LEAK' -> action 'syslog_only'", id='critical_syslog_only'),
    pytest.param(False, 'power_off',
                 r"SYSTEM_LEAK handler done: .*result=SKIPPED \(policy=disabled\)", id='policy_disabled'),
])
def test_critical_leak_action_configuration(bmc, switch_host, leak_mocker, leak_policy,
                                            policy_enabled, critical_action, expected_log):
    """A configured non-power action, or a disabled policy, leaves the Switch-Host up on a CRITICAL leak."""
    set_system_leak_action(bmc, 'critical', critical_action)
    set_system_leak_policy(bmc, policy_enabled)
    policy = get_leak_control_policy(bmc)
    expected_policy = 'enabled' if policy_enabled else 'disabled'
    pytest_assert(policy.get('system_critical_leak_action') == critical_action
                  and policy.get('system_leak_policy') == expected_policy,
                  "CLI did not apply the leak policy, CONFIG_DB has {}".format(policy))

    index = _sensor_for(leak_mocker, 'CRITICAL')
    pre_uptime = get_host_uptime(switch_host)
    la = make_bmc_loganalyzer(bmc, "leak_e2e_action_{}".format(critical_action if policy_enabled else 'disabled'))
    marker = la.init(log_target='syslog')
    try:
        leak_mocker.set_sensor_state(index, 'CRITICAL')
        pytest_assert(wait_system_leak_status(bmc, 'CRITICAL', DETECTION_TIMEOUT_SEC, _poll_sec(bmc)),
                      "SYSTEM_LEAK_STATUS did not become CRITICAL, got {!r}".format(get_system_leak_status(bmc)))
        context = ("on a CRITICAL leak with action syslog_only" if policy_enabled
                   else "on a CRITICAL leak with the system leak policy disabled")
        _assert_host_stayed_up(switch_host, pre_uptime, context)
        oper_status = get_switch_host_oper_status(bmc)
        pytest_assert(oper_status == 'online',
                      "BMC reports the Switch-Host {!r} {}".format(oper_status, context))
        _assert_logged(_matched_lines(la, marker, [expected_log], 'syslog'), expected_log, "bmcctld")
    finally:
        # Clear before the policy is restored: re-enabling power_off with the leak still latched
        # would dispatch on the next status write.
        leak_mocker.set_sensor_state(index, 'None')
    _wait_cleared(bmc, leak_mocker, index)
