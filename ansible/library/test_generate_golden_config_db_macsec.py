"""Unit tests for the macsec_links handling in generate_golden_config_db.py.

Run from the repo root with:
    python -m pytest --noconftest ansible/library/test_generate_golden_config_db_macsec.py -v

ansible and sonic_py_common are stubbed so the module loads without a DUT.
"""

import copy
import importlib.util
import json
import os
import sys
import types

import pytest


class ModuleFailed(Exception):
    def __init__(self, results):
        super(ModuleFailed, self).__init__(results.get("msg"))
        self.results = results


class FakeAnsibleModule(object):
    def __init__(self):
        self.warnings = []

    def warn(self, msg):
        self.warnings.append(msg)

    def fail_json(self, **kwargs):
        raise ModuleFailed(kwargs)

    def run_command(self, command, **kwargs):
        raise AssertionError("unexpected run_command: {}".format(command))


def _load_golden_config_module():
    ansible = types.ModuleType("ansible")
    module_utils = types.ModuleType("ansible.module_utils")
    basic = types.ModuleType("ansible.module_utils.basic")
    basic.AnsibleModule = object
    smartswitch_utils = types.ModuleType("ansible.module_utils.smartswitch_utils")
    smartswitch_utils.smartswitch_hwsku_config = {}
    smartswitch_utils.smartswitch_vlan_config = {}
    sonic_py_common = types.ModuleType("sonic_py_common")
    sonic_py_common.device_info = types.ModuleType("sonic_py_common.device_info")
    sonic_py_common.multi_asic = types.ModuleType("sonic_py_common.multi_asic")

    fake_modules = {
        "ansible": ansible,
        "ansible.module_utils": module_utils,
        "ansible.module_utils.basic": basic,
        "ansible.module_utils.smartswitch_utils": smartswitch_utils,
        "sonic_py_common": sonic_py_common,
        "sonic_py_common.device_info": sonic_py_common.device_info,
        "sonic_py_common.multi_asic": sonic_py_common.multi_asic,
    }
    saved_modules = {}
    for name, module in fake_modules.items():
        saved_modules[name] = sys.modules.get(name)
        sys.modules[name] = module

    try:
        module_path = os.path.join(os.path.dirname(__file__), "generate_golden_config_db.py")
        spec = importlib.util.spec_from_file_location("generate_golden_config_db", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, module in saved_modules.items():
            if module is None:
                del sys.modules[name]
            else:
                sys.modules[name] = module


gcdb = _load_golden_config_module()
MACSEC_MTU = gcdb.MACSEC_MTU

PROFILE = "MACSEC_PROFILE_A"
PROFILE_BODY = {"priority": "64", "cipher_suite": "GCM-AES-XPN-256", "policy": "security"}
FEATURE_ROWS = {
    "bgp": {"state": "enabled"},
    "macsec": {"state": "disabled", "auto_restart": "enabled"},
}


def _minigraph_ns(neighbors, pc_members=None, ports=None, portchannels=None):
    """Shape of one namespace of `sonic-cfggen -m --print-data` output."""
    return {
        "DEVICE_NEIGHBOR": {port: {"name": vm, "port": "Ethernet1"} for port, vm in neighbors.items()},
        "PORT": ports or {port: {"lanes": "0", "admin_status": "up"} for port in neighbors},
        "PORTCHANNEL": portchannels or {},
        "PORTCHANNEL_MEMBER": pc_members or {},
        "FEATURE": copy.deepcopy(FEATURE_ROWS),
    }


def _multiasic_minigraph():
    """asic0 is a frontend owning Ethernet0 (to VM0100, in PortChannel101) and
    Ethernet4 (to a VM without macsec); asic1 is a backend facing asic0 only."""
    return {
        "localhost": {"FEATURE": copy.deepcopy(FEATURE_ROWS), "DEVICE_METADATA": {"localhost": {}}},
        "asic0": _minigraph_ns(
            {"Ethernet0": "VM0100", "Ethernet4": "VM0101"},
            pc_members={"PortChannel101|Ethernet0": {}, "PortChannel102|Ethernet4": {}},
            portchannels={"PortChannel101": {"admin_status": "up"},
                          "PortChannel102": {"admin_status": "up"}}),
        "asic1": _minigraph_ns(
            {"Ethernet-BP0": "ASIC0"},
            ports={"Ethernet-BP0": {"lanes": "8", "admin_status": "up"}}),
    }


def _multiasic_profile_render(num_asics=2):
    """Shape golden_config_db_t2.j2 renders when asic_cnt > 1."""
    rendered = {"localhost": {"MACSEC_PROFILE": {}}}
    for asic in range(num_asics):
        rendered["asic{}".format(asic)] = {"MACSEC_PROFILE": {PROFILE: dict(PROFILE_BODY)}}
    return rendered


def make_generator(num_asics=2, macsec_links=None, minigraph=None, profile_render=None):
    gen = gcdb.GenerateGoldenConfigDBModule.__new__(gcdb.GenerateGoldenConfigDBModule)
    gen.module = FakeAnsibleModule()
    gen.num_asics = num_asics
    gen.macsec_profile = PROFILE
    gen.macsec_links = macsec_links if macsec_links is not None else [{"vm": "VM0100"}]
    if num_asics > 1:
        mg = minigraph if minigraph is not None else _multiasic_minigraph()
        render = profile_render if profile_render is not None else _multiasic_profile_render(num_asics)
        gen.get_config_from_minigraph_multiasic = lambda: copy.deepcopy(mg)
    else:
        mg = minigraph
        render = profile_render
        gen.get_config_from_minigraph = lambda: json.dumps(mg)
    gen._render_macsec_profile_table = lambda: copy.deepcopy(render)
    return gen


def test_flat_config_is_hoisted_under_localhost():
    flat = {"DEVICE_METADATA": {"localhost": {"hostname": "dut"}}, "SYSLOG_CONFIG": {"x": {}}}
    gen = make_generator()

    result = gen._apply_macsec_links_multiasic(copy.deepcopy(flat))

    assert set(result) == {"localhost", "asic0", "asic1"}
    for table, rows in flat.items():
        assert result["localhost"][table] == rows
    assert "DEVICE_METADATA" not in result["asic0"]


def test_namespace_keyed_config_is_not_rehoisted():
    keyed = {
        "localhost": {"DEVICE_METADATA": {"localhost": {"hostname": "dut"}}},
        "asic0": {"BGP_DEVICE_GLOBAL": {"STATE": {"tsa_enabled": "false"}}},
    }
    gen = make_generator()

    result = gen._apply_macsec_links_multiasic(copy.deepcopy(keyed))

    assert "localhost" not in result["localhost"]
    assert set(result) == {"localhost", "asic0", "asic1"}
    assert result["localhost"]["DEVICE_METADATA"] == keyed["localhost"]["DEVICE_METADATA"]
    assert result["asic0"]["BGP_DEVICE_GLOBAL"] == keyed["asic0"]["BGP_DEVICE_GLOBAL"]


def test_asic_keyed_config_without_localhost_is_not_rehoisted():
    gen = make_generator()

    result = gen._apply_macsec_links_multiasic({"asic0": {"PORT": {}}})

    assert "asic0" not in result.get("localhost", {})
    assert "PORT" in result["asic0"]


def test_bindings_land_only_in_owning_asic_namespace():
    gen = make_generator()

    result = gen._apply_macsec_links_multiasic({})
    asic0, asic1, host = result["asic0"], result["asic1"], result["localhost"]

    assert asic0["PORT"]["Ethernet0"]["macsec"] == PROFILE
    assert asic0["PORT"]["Ethernet0"]["mtu"] == MACSEC_MTU
    # The rest of the minigraph PORT table is carried so table replacement keeps it.
    assert asic0["PORT"]["Ethernet0"]["lanes"] == "0"
    assert asic0["PORT"]["Ethernet4"] == {"lanes": "0", "admin_status": "up"}
    assert asic0["PORTCHANNEL"]["PortChannel101"]["mtu"] == MACSEC_MTU
    assert asic0["PORTCHANNEL"]["PortChannel102"] == {"admin_status": "up"}
    assert asic0["MACSEC_PROFILE"] == {PROFILE: PROFILE_BODY}

    # Backend namespace: profile rendered, no port or PortChannel binding.
    assert asic1["MACSEC_PROFILE"] == {PROFILE: PROFILE_BODY}
    assert "PORT" not in asic1
    assert "PORTCHANNEL" not in asic1

    # The host namespace owns no front-panel ports.
    for table in ("PORT", "PORTCHANNEL", "MACSEC_PROFILE"):
        assert table not in host
    assert gen.module.warnings == []


def test_link_profile_overrides_global_profile_per_port():
    other = "MACSEC_PROFILE_B"
    render = _multiasic_profile_render()
    for ns in ("asic0", "asic1"):
        render[ns]["MACSEC_PROFILE"][other] = dict(PROFILE_BODY)
    gen = make_generator(macsec_links=[{"vm": "VM0100", "profile": other}], profile_render=render)

    result = gen._apply_macsec_links_multiasic({})

    assert result["asic0"]["PORT"]["Ethernet0"]["macsec"] == other
    assert "macsec" not in result["asic0"]["PORT"]["Ethernet4"]


def test_feature_macsec_enabled_in_every_namespace():
    gen = make_generator()

    result = gen._apply_macsec_links_multiasic({})

    for ns in ("localhost", "asic0", "asic1"):
        assert result[ns]["FEATURE"]["macsec"]["state"] == "enabled"
        assert result[ns]["FEATURE"]["macsec"]["auto_restart"] == "enabled"
        assert result[ns]["FEATURE"]["bgp"] == {"state": "enabled"}


def test_unmatched_vms_warn_once():
    gen = make_generator(macsec_links=[{"vm": "VM9999"}])

    result = gen._apply_macsec_links_multiasic({})

    assert len(gen.module.warnings) == 1
    assert "VM9999" in gen.module.warnings[0]
    for ns in ("asic0", "asic1"):
        assert "PORT" not in result[ns]
        assert result[ns]["FEATURE"]["macsec"]["state"] == "enabled"


def test_apply_macsec_links_multiasic_entry():
    flat = {"DEVICE_METADATA": {"localhost": {"hostname": "dut"}}}
    gen = make_generator()

    result = json.loads(gen.apply_macsec_links(json.dumps(flat)))

    assert result["localhost"]["DEVICE_METADATA"] == flat["DEVICE_METADATA"]
    assert result["asic0"]["PORT"]["Ethernet0"]["macsec"] == PROFILE
    assert "PORT" not in result
    assert "MACSEC_PROFILE" not in result


def test_apply_macsec_links_single_asic_binds_top_level():
    minigraph = _minigraph_ns(
        {"Ethernet0": "VM0100", "Ethernet4": "VM0101"},
        pc_members={"PortChannel101|Ethernet0": {}},
        portchannels={"PortChannel101": {"admin_status": "up"}})
    render = {"MACSEC_PROFILE": {PROFILE: dict(PROFILE_BODY)}}
    gen = make_generator(num_asics=1, minigraph=minigraph, profile_render=render)

    def no_multiasic():
        raise AssertionError("single-ASIC must not query per-namespace minigraph")
    gen.get_config_from_minigraph_multiasic = no_multiasic

    result = json.loads(gen.apply_macsec_links(json.dumps({})))

    assert "localhost" not in result and "asic0" not in result
    assert result["MACSEC_PROFILE"] == {PROFILE: PROFILE_BODY}
    assert result["PORT"]["Ethernet0"]["macsec"] == PROFILE
    assert result["PORT"]["Ethernet0"]["mtu"] == MACSEC_MTU
    assert "macsec" not in result["PORT"]["Ethernet4"]
    assert result["PORTCHANNEL"]["PortChannel101"]["mtu"] == MACSEC_MTU
    assert result["FEATURE"]["macsec"]["state"] == "enabled"
    assert gen.module.warnings == []


def test_missing_profile_in_asic_namespace_fails():
    render = _multiasic_profile_render()
    render["asic0"]["MACSEC_PROFILE"] = {}
    gen = make_generator(profile_render=render)

    with pytest.raises(ModuleFailed) as exc:
        gen._apply_macsec_links_multiasic({})
    assert PROFILE in exc.value.results["msg"]
