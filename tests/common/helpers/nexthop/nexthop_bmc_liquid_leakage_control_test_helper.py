"""
Nexthop SONiC-BMC liquid leakage mocker.

On Nexthop liquid-cooled chassis (NH-4240L Marlin Dolphin) the switch-card FPGA exposes a
leak-injection register ("Leak Sensor Test 0", 0x012C: bit 31 enable, bits [1:0] domain
selector) that drives the REAL leak comparator of the selected domain. An injection therefore
propagates exactly as a wet sensor does:

    FPGA -> LEAK_FAULT line -> BMC GPIO -> thermalctld -> STATE_DB -> bmcctld policy action

The register is only reachable over the Switch-Host's PCIe, so the mocker runs the `fpga`
tool on the paired host (duthost.get_bmc_host()). Hardware facts the tests must live with:

- one domain is injected at a time (single selector);
- the severity of each BMC sensor is fixed by the wiring: both cold plates feed the MINOR
  fault line, the drip-tray ropes feed the CRITICAL fault line;
- a sensor-fault (SNS_OK) override never reaches the BMC, so the FAULT state cannot be mocked;
- a CRITICAL injection powers the Switch-Host off (default policy). The injection then cannot
  be cleared until the host is back, and bmcctld refuses to power the host on while the leak
  is CRITICAL, so restore_all_sensors() recovers the host with the system leak policy disabled.
"""

import logging
import time

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.bmc_liquid_leakage_control_test_helper import LiquidLeakageMockerBMC
from tests.common.helpers.sensor_control_test_helper import mocker
from tests.common.platform.bmc_utils import (
    get_leak_control_policy,
    get_system_leak_status,
    recover_switch_host_after_power_off,
    restore_leak_control_policy,
    set_system_leak_policy,
    wait_system_leak_status,
)
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

FPGA_DEVICE = 'SWITCHCARD_FPGA'
LEAK_TEST_REGISTER = 0x012C
LEAK_TEST_ENABLE = 1 << 31

# BMC leak sensor type (from `show platform leak status`) -> FPGA injection selectors wired to
# that sensor's fault line, and the severity the line carries. The first selector is used.
SENSOR_WIRING = {
    'FlexPCB': {'selectors': (0, 1), 'severity': 'MINOR'},   # cpu / switch-card cold plates
    'Rope': {'selectors': (3, 2), 'severity': 'CRITICAL'},   # drip-tray rope 1 / rope 2
}

DETECTION_TIMEOUT_SEC = 10
CLEAR_TIMEOUT_SEC = 15
POLL_SEC = 0.5


@mocker('LiquidLeakageMockerBMC')
class LiquidLeakageMockerNexthopBMC(LiquidLeakageMockerBMC):
    """Drive the Nexthop switch-card FPGA leak-injection register from the paired Switch-Host."""

    def __init__(self, dut):
        super().__init__(dut)
        self.host = dut.get_bmc_host()
        self._wiring = {}
        for name, info in self.sensors.items():
            wiring = SENSOR_WIRING.get(info.get('leak_sensor_type'))
            if wiring is None:
                raise ValueError("No FPGA injection wiring for leak sensor {} of type {!r}".format(
                    name, info.get('leak_sensor_type')))
            self._wiring[name] = wiring
        self._injected_name = None
        self._clear_stale_injection()

    # --- FPGA register access (Switch-Host side) ---------------------------

    def _fpga_cmd(self, verb, *args):
        return "fpga {} {} 0x{:04X} {}".format(verb, FPGA_DEVICE, LEAK_TEST_REGISTER, " ".join(args)).strip()

    def _read(self):
        out = self.host.shell(self._fpga_cmd('read32'))['stdout'].strip()
        return int(out, 16)

    def _write(self, value, host_may_drop=False):
        """Write the register and read it back in one SSH session.

        A CRITICAL injection powers the Switch-Host off well under a second after the write, so the
        readback shares the session with the write and a connection lost right after a CRITICAL
        injection is the expected effect, not an error.
        """
        cmd = "{} && {}".format(self._fpga_cmd('write32', "0x{:08X}".format(value)), self._fpga_cmd('read32'))
        try:
            out = self.host.shell(cmd)['stdout'].strip().splitlines()[-1]
        except Exception as e:
            if not host_may_drop:
                raise
            logger.info("Switch-Host dropped right after the leak injection, before the readback: %s", e)
            return
        readback = int(out, 16)
        pytest_assert(readback == value,
                      "Leak test register readback 0x{:08x} != written 0x{:08x}".format(readback, value))

    def _clear_stale_injection(self):
        out = self.host.shell(self._fpga_cmd('read32'), module_ignore_errors=True)
        pytest_assert(out.get('rc') == 0,
                      "Cannot read the leak test register on {}: {}".format(
                          self.host.hostname, (out.get('stderr') or out.get('stdout') or '').strip()))
        value = int(out['stdout'].strip(), 16)
        if value & LEAK_TEST_ENABLE:
            logger.warning("Leak injection already active (0x%08x) before the test; clearing it", value)
            self._write(0)

    def host_reachable(self):
        """True when the Switch-Host answers a command over SSH."""
        try:
            return self.host.shell("true", module_ignore_errors=True).get('rc') == 0
        except Exception as e:
            logger.info("Switch-Host %s not reachable: %s", self.host.hostname, e)
            return False

    # --- Capabilities ------------------------------------------------------

    def sensor_severity(self, index):
        """The severity the sensor's fault line carries; the only leak state it can report."""
        return self._wiring[self.get_sensor_name(index)]['severity']

    def sensors_supporting(self, state):
        """Indices of the sensors that can be driven to `state` ('None', 'MINOR' or 'CRITICAL')."""
        return [index for index, name in enumerate(self.sensor_names)
                if state == 'None' or self._wiring[name]['severity'] == state]

    # --- LiquidLeakageMockerBMC interface ----------------------------------

    def set_sensor_state(self, index, state):
        self._validate_sensor_state(state)
        name = self.get_sensor_name(index)
        if state == 'None':
            if self._injected_name is not None:
                self.restore_all_sensors()
            return
        if state == 'FAULT':
            pytest.skip("A leak sensor fault (SNS_OK override) never reaches the BMC on this platform")
        severity = self._wiring[name]['severity']
        if state != severity:
            pytest.skip("{} is wired to the {} leak-fault line and cannot report {}".format(name, severity, state))
        if self._injected_name not in (None, name):
            raise RuntimeError("{} is already injected; the FPGA injects one leak domain at a time".format(
                self._injected_name))

        selector = self._wiring[name]['selectors'][0]
        started = time.monotonic()
        self._write(LEAK_TEST_ENABLE | selector, host_may_drop=(severity == 'CRITICAL'))
        self._injected_name = name
        pytest_assert(
            wait_until(DETECTION_TIMEOUT_SEC, POLL_SEC, 0, lambda: self.get_sensor_status(index)[0]),
            "BMC did not report {} leaking within {}s of the FPGA injection (selector {})".format(
                name, DETECTION_TIMEOUT_SEC, selector))
        logger.info("%s reported leaking %.1fs after the FPGA injection (selector %d)",
                    name, time.monotonic() - started, selector)

    def restore_all_sensors(self):
        """Clear the injection, bringing the Switch-Host back first if the leak powered it off.

        bmcctld blocks power_on while SYSTEM_LEAK_STATUS is CRITICAL and the system leak policy
        is enabled, and only the host can clear the register. So with the host down: disable the
        policy, power the host on through a clean shutdown/startup, clear the register, wait for
        thermalctld to clear the status, then restore the policy.
        """
        if self.host_reachable():
            self._clear()
            return
        logger.warning("Switch-Host %s is down after the leak; recovering it with the system leak policy disabled",
                       self.host.hostname)
        snapshot = get_leak_control_policy(self.dut)
        try:
            set_system_leak_policy(self.dut, False)
            recover_switch_host_after_power_off(self.dut, self.host, context="after FPGA leak injection")
            self._clear()
        finally:
            restore_leak_control_policy(self.dut, snapshot)

    def _clear(self):
        self._write(0)
        self._injected_name = None
        pytest_assert(
            wait_system_leak_status(self.dut, 'None', CLEAR_TIMEOUT_SEC, POLL_SEC),
            "SYSTEM_LEAK_STATUS still {!r} {}s after clearing the FPGA injection".format(
                get_system_leak_status(self.dut), CLEAR_TIMEOUT_SEC))
