"""
Ad-hoc validation for sonic-net/sonic-mgmt#26931 on the 202605 branch.

Not part of the cherry-pick PR. Exercises, on a DAC-cabled testbed whose
deploy-mg golden config carries PORT.<intf>.link_training:

  00  baseline after deploy-mg: golden == .origin.backup, CONFIG_DB and the
      saved config carry link_training on every golden port, links trained
  01  plain `config load_minigraph -y` (what a test-time revert does without
      this change): link_training and trained drop to 0 -- the premise
  02  config_reload(config_source='minigraph') without override: the reapply
      helper logs "Re-applying link_training ... on N ports", CONFIG_DB and
      saved config carry it again, links trained
  03  config_reload(config_source='minigraph', override_config=True): the -o
      path, no reapply log line, same end state
"""
import json
import logging
import re

import pytest

from tests.common.config_reload import config_reload
from tests.common.constants import GOLDEN_CONFIG_DB_PATH, GOLDEN_CONFIG_DB_PATH_ORI
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer,
]

logger = logging.getLogger(__name__)

REAPPLY_RE = re.compile(r"Re-applying link_training from golden config on (\d+) ports after minigraph reload")
LT_ROW_RE = re.compile(r"^\s*(Ethernet\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")

TRAIN_TIMEOUT = 600
TRAIN_INTERVAL = 20


def _json_file(duthost, path):
    res = duthost.shell("cat {}".format(path), module_ignore_errors=True)
    if res["rc"] != 0:
        return None
    return json.loads(res["stdout"])


def _lt_ports_in(config):
    ports = (config or {}).get("PORT", {})
    return {p: a.get("link_training") for p, a in ports.items()
            if isinstance(a, dict) and a.get("link_training") in ("on", "off")}


def _config_db_lt(duthost):
    cmd = ('for k in $(sonic-db-cli CONFIG_DB keys "PORT|*"); do '
           'echo "$k $(sonic-db-cli CONFIG_DB hget "$k" link_training)"; done')
    out = duthost.shell(cmd, executable="/bin/bash")["stdout_lines"]
    result = {}
    for line in out:
        parts = line.split()
        if len(parts) == 2:
            result[parts[0].split("|", 1)[1]] = parts[1]
    return result


def _lt_status(duthost):
    """{port: (lt_oper, lt_admin, oper, admin)} from `show interfaces link-training status`."""
    out = duthost.shell("show interfaces link-training status")["stdout_lines"]
    rows = {}
    for line in out:
        m = LT_ROW_RE.match(line)
        if m:
            rows[m.group(1)] = m.group(2, 3, 4, 5)
    return rows


def _summary(duthost, expected):
    """expected = (lt_ports, active_ports): golden LT-on ports, and the admin-up subset that must train."""
    lt_ports, active = expected
    cdb = _config_db_lt(duthost)
    st = _lt_status(duthost)
    on_cdb = sorted(p for p in lt_ports if cdb.get(p) == "on")
    trained = sorted(p for p in active if st.get(p, ("",))[0] == "trained")
    up = sorted(p for p in active if st.get(p, ("", "", ""))[2] == "up")
    saved = _lt_ports_in(_json_file(duthost, "/etc/sonic/config_db.json"))
    saved_on = sorted(p for p in lt_ports if saved.get(p) == "on")
    bgp_globals = duthost.shell('sonic-db-cli CONFIG_DB keys "BGP_GLOBALS|*"')["stdout_lines"]
    s = dict(golden_lt_ports=len(lt_ports), active_ports=len(active), config_db_lt_on=len(on_cdb),
             trained=len(trained), oper_up=len(up), saved_config_lt_on=len(saved_on),
             bgp_globals_keys=len(bgp_globals))
    logger.info("LT summary: %s", s)
    return s, {"not_trained": sorted(set(active) - set(trained)), "not_on": sorted(set(lt_ports) - set(on_cdb))}


def _all_trained(duthost, expected):
    lt_ports, active = expected
    s, _ = _summary(duthost, expected)
    return s["trained"] == len(active) and s["config_db_lt_on"] == len(lt_ports)


def _none_trained(duthost, expected):
    s, _ = _summary(duthost, expected)
    return s["trained"] == 0 and s["config_db_lt_on"] == 0


@pytest.fixture(scope="module")
def lt_expected(duthosts, rand_one_dut_hostname):
    duthost = duthosts[rand_one_dut_hostname]
    golden = _json_file(duthost, GOLDEN_CONFIG_DB_PATH)
    pytest_assert(golden is not None, "no golden config at {}".format(GOLDEN_CONFIG_DB_PATH))
    lt = _lt_ports_in(golden)
    expected = sorted(p for p, v in lt.items() if v == "on")
    logger.info("Golden config carries link_training on %d ports, off %d ports",
                len(expected), sum(1 for v in lt.values() if v == "off"))
    pytest_assert(expected, "golden config carries no PORT.link_training=on; nothing to validate")
    st = _lt_status(duthost)
    active = sorted(p for p in expected if st.get(p, ("", "", "", ""))[3] == "up")
    logger.info("%d of those ports are admin up (topology ports) and must train: %s", len(active), active)
    pytest_assert(active, "none of the golden link_training ports is admin up")
    return expected, active


def test_00_baseline_after_deploy_mg(duthosts, rand_one_dut_hostname, lt_expected):
    duthost = duthosts[rand_one_dut_hostname]
    logger.info("Image: %s", duthost.shell("show version | head -3")["stdout"])
    cmp = duthost.shell("cmp {} {}".format(GOLDEN_CONFIG_DB_PATH, GOLDEN_CONFIG_DB_PATH_ORI),
                        module_ignore_errors=True)
    pytest_assert(cmp["rc"] == 0, "golden config differs from .origin.backup: {}".format(cmp["stdout"]))
    pytest_assert(wait_until(TRAIN_TIMEOUT, TRAIN_INTERVAL, 0, _all_trained, duthost, lt_expected),
                  "baseline: not all golden LT ports are link_training on + trained: {}".format(
                      _summary(duthost, lt_expected)))
    s, _ = _summary(duthost, lt_expected)
    pytest_assert(s["saved_config_lt_on"] == len(lt_expected[0]), "saved config_db.json missing link_training")


def test_01_plain_load_minigraph_drops_lt(duthosts, rand_one_dut_hostname, lt_expected):
    """Premise: the override-free revert a test performs drops link training."""
    duthost = duthosts[rand_one_dut_hostname]
    duthost.shell("sudo config load_minigraph -y", executable="/bin/bash")
    pytest_assert(wait_until(TRAIN_TIMEOUT, TRAIN_INTERVAL, 30, _none_trained, duthost, lt_expected),
                  "plain load_minigraph kept link training: {}".format(_summary(duthost, lt_expected)))


def test_02_config_reload_minigraph_reapplies_lt(duthosts, rand_one_dut_hostname, lt_expected, caplog):
    duthost = duthosts[rand_one_dut_hostname]
    caplog.set_level(logging.INFO, logger="tests.common.config_reload")
    config_reload(duthost, config_source="minigraph", safe_reload=False, check_intf_up_ports=False,
                  override_config=False)
    hits = [m for r in caplog.records for m in [REAPPLY_RE.search(r.getMessage())] if m]
    pytest_assert(hits, "no 'Re-applying link_training' log line after non-override minigraph reload")
    pytest_assert(int(hits[-1].group(1)) == len(lt_expected[0]),
                  "reapply covered {} ports, golden has {}".format(hits[-1].group(1), len(lt_expected[0])))
    logger.info("reapply log: %s", hits[-1].group(0))
    pytest_assert(wait_until(TRAIN_TIMEOUT, TRAIN_INTERVAL, 0, _all_trained, duthost, lt_expected),
                  "after non-override reload: {}".format(_summary(duthost, lt_expected)))
    s, _ = _summary(duthost, lt_expected)
    pytest_assert(s["saved_config_lt_on"] == len(lt_expected[0]), "config save did not persist link_training")


def test_03_config_reload_minigraph_override_unchanged(duthosts, rand_one_dut_hostname, lt_expected, caplog):
    duthost = duthosts[rand_one_dut_hostname]
    caplog.set_level(logging.INFO, logger="tests.common.config_reload")
    config_reload(duthost, config_source="minigraph", safe_reload=False, check_intf_up_ports=False,
                  override_config=True)
    hits = [r for r in caplog.records if REAPPLY_RE.search(r.getMessage())]
    pytest_assert(not hits, "reapply ran on the override (-o) path: {}".format([r.getMessage() for r in hits]))
    pytest_assert(wait_until(TRAIN_TIMEOUT, TRAIN_INTERVAL, 0, _all_trained, duthost, lt_expected),
                  "after override reload: {}".format(_summary(duthost, lt_expected)))
    s, _ = _summary(duthost, lt_expected)
    pytest_assert(s["saved_config_lt_on"] == len(lt_expected[0]), "config save did not persist link_training")
    cmp = duthost.shell("cmp {} {}".format(GOLDEN_CONFIG_DB_PATH, GOLDEN_CONFIG_DB_PATH_ORI),
                        module_ignore_errors=True)
    pytest_assert(cmp["rc"] == 0, "golden config no longer matches .origin.backup")
