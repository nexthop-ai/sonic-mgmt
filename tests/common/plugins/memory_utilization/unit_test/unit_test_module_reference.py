import unittest
from unittest.mock import MagicMock

from tests.common.plugins import memory_utilization as plugin
from tests.common.plugins.memory_utilization.memory_utilization import MemoryMonitor

MEMORY_PARAMS = {"used": {"memory_increase_threshold": {"type": "percentage", "value": "20%"}}}
TWO_TIER_PARAMS = {"used": {"memory_increase_threshold": {"type": "percentage", "value": "20%"},
                            "memory_increase_fail_threshold": {"type": "percentage", "value": "50%"}}}
CHECKER_LOGGER = "tests.common.plugins.memory_utilization.memory_utilization"


def item(nodeid):
    return MagicMock(nodeid=nodeid)


def duthost():
    host = MagicMock(hostname="dut1", topo_type="t0")
    host.is_bmc.return_value = False
    host.facts = {"asic_type": "broadcom"}
    return host


def monitor(memory_params=MEMORY_PARAMS, host=None):
    mon = MemoryMonitor(host or duthost())
    mon.register_command("free", "free -m", memory_params, lambda output, params: {"used": float(output)})
    return mon


class TestModuleReference(unittest.TestCase):
    def setUp(self):
        plugin._module_reference.update(module=None, values={})
        plugin._memory_errors_by_test.clear()

    def test_keeps_highest_value_within_module(self):
        plugin._update_module_reference(item("tests/a/test_a.py::test_1"), "dut1", {"free": {"used": 4000.0}})
        plugin._update_module_reference(item("tests/a/test_a.py::test_2"), "dut1", {"free": {"used": 2500.0}})
        self.assertEqual(plugin._module_reference["values"], {"dut1": {"free": {"used": 4000.0}}})

    def test_resets_when_module_changes(self):
        plugin._update_module_reference(item("tests/a/test_a.py::test_1"), "dut1", {"free": {"used": 4000.0}})
        plugin._update_module_reference(item("tests/b/test_b.py::test_1"), "dut1", {"free": {"used": 2500.0}})
        self.assertEqual(plugin._module_reference["module"], "tests/b/test_b.py")
        self.assertEqual(plugin._module_reference["values"], {"dut1": {"free": {"used": 2500.0}}})

    def test_return_to_reference_is_not_an_increase(self):
        mon = monitor()
        mon.check_memory_thresholds({"free": {"used": 4500.0}}, {"free": {"used": 2600.0}},
                                    reference_values={"free": {"used": 4500.0}})
        self.assertEqual(mon.get_memory_errors(), [])

    def test_growth_above_reference_alarms_and_names_both_values(self):
        mon = monitor()
        mon.check_memory_thresholds({"free": {"used": 6000.0}}, {"free": {"used": 2600.0}},
                                    reference_values={"free": {"used": 4500.0}})
        self.assertEqual(len(mon.get_memory_errors()), 1)
        self.assertIn("increased by 33.3%", mon.get_memory_errors()[0])
        self.assertIn("previous: 2600.0 MB", mon.get_memory_errors()[0])
        self.assertIn("measured from module reference 4500.0 MB", mon.get_memory_errors()[0])

    def test_reference_below_pre_test_value_is_ignored(self):
        mon = monitor()
        mon.check_memory_thresholds({"free": {"used": 4500.0}}, {"free": {"used": 2600.0}},
                                    reference_values={"free": {"used": 2000.0}})
        self.assertEqual(len(mon.get_memory_errors()), 1)
        self.assertNotIn("module reference", mon.get_memory_errors()[0])

    def test_without_reference_behaves_as_before(self):
        mon = monitor()
        mon.check_memory_thresholds({"free": {"used": 4500.0}}, {"free": {"used": 2600.0}})
        self.assertEqual(len(mon.get_memory_errors()), 1)

    def test_two_tier_thresholds_use_reference_base(self):
        mon = monitor(TWO_TIER_PARAMS)
        mon.check_memory_thresholds({"free": {"used": 5000.0}}, {"free": {"used": 2600.0}},
                                    reference_values={"free": {"used": 4500.0}})
        self.assertEqual(mon.get_memory_errors(), [])
        with self.assertLogs(CHECKER_LOGGER, level="WARNING") as logs:
            mon.check_memory_thresholds({"free": {"used": 5500.0}}, {"free": {"used": 2600.0}},
                                        reference_values={"free": {"used": 4500.0}})
        self.assertEqual(mon.get_memory_errors(), [])
        self.assertIn("[WARNING]: free:used memory usage increased by 1000.0 MB", logs.output[0])
        self.assertIn("measured from module reference 4500.0 MB", logs.output[0])
        mon.check_memory_thresholds({"free": {"used": 7000.0}}, {"free": {"used": 2600.0}},
                                    reference_values={"free": {"used": 4500.0}})
        self.assertEqual(len(mon.get_memory_errors()), 1)
        self.assertIn("measured from module reference 4500.0 MB", mon.get_memory_errors()[0])

    def test_hooks_measure_from_module_reference(self):
        host = duthost()
        host.command.side_effect = [{"stdout": out} for out in ("4500", "4500", "2600", "4500")]
        memory_values = {"before_test": {"dut1": {}}, "after_test": {"dut1": {}}}
        config = MagicMock(store_duthosts=[host],
                           store_memory_utilization=({"dut1": monitor(host=host)}, memory_values))
        config.getoption.return_value = False
        for name in ("test_1", "test_2"):
            it = MagicMock(nodeid="tests/a/test_a.py::" + name, fixturenames=[], keywords={}, config=config)
            it.rep_setup.skipped = it.rep_call.skipped = False
            plugin.pytest_runtest_setup(it)
            plugin.pytest_runtest_teardown(it, None)
        self.assertEqual(plugin._memory_errors_by_test, {})


if __name__ == "__main__":
    unittest.main()
