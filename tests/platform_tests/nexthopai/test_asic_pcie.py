"""
Nexthop AI platform test for ASIC boot flash accessibility.

This test validates two access paths:
1) ASIC/SDK path: read ASIC PCIe FW version via `asic_pcie_fw_version` for ASIC_PCIE* components
2) FPGA/PDDF path: create SPI device(s) via pddfparse and read from /dev/mtd*

The FPGA path temporarily switches the mux/grab-bit so the ASIC/SDK path will not work
until the SPI device is deleted again. The test ensures cleanup on exit.

Failure conditions:
- ASIC/SDK path:
  - expected `ASIC_PCIE*` components are missing, or
  - `asic_pcie_fw_version` fails for any component, or
  - any `ASIC_PCIE*` version is empty / `N/A` / `UNKNOWN` / `UNKNOWN_TIMEOUT`.
- FPGA/PDDF path:
  - fails to create `ASIC_BOOT_FLASH*` subtree with `pddfparse.py`, or
  - flash content sample is all 0xff or all 0x00.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass

import pytest

from tests.common.helpers.assertions import pytest_assert

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology("any"),
]


@dataclass(frozen=True)
class _CmdResult:
    cmd: str
    rc: int
    stdout: str
    stderr: str


def _with_timeout(cmd: str, seconds: int) -> str:
    # Use coreutils timeout when available to avoid hanging tests.
    return f"timeout {seconds}s {cmd}"


def _run(duthost, cmd: str) -> _CmdResult:
    res = duthost.shell(cmd, module_ignore_errors=True)
    return _CmdResult(
        cmd=cmd,
        rc=int(res.get("rc", 1)),
        stdout=(res.get("stdout") or ""),
        stderr=(res.get("stderr") or ""),
    )


def _extract_asic_pcie_components_from_facts(duthost) -> list[tuple[str, int | None]]:
    """
    Extract ASIC_PCIE* component names/suffix from duthost facts.
    """
    chassis = duthost.facts.get("chassis") or {}
    components = chassis.get("components")
    if not components:
        pytest.fail("Missing `duthost.facts['chassis']['components']`; cannot enumerate platform components. ")
    out: list[tuple[str, int | None]] = []
    for comp in components:
        name = (comp or {}).get("name")
        if not name:
            continue
        m = re.fullmatch(r"ASIC_PCIE(?:_D(\d+))?", name)
        if not m:
            continue
        suffix = int(m.group(1)) if m.group(1) is not None else None
        out.append((name, suffix))

    if not out:
        pytest.fail(
            "No ASIC PCIe components found in `duthost.facts['chassis']['components']`. "
            "Expected at least one of: ASIC_PCIE / ASIC_PCIE_D<idx>."
        )
    return out


def _extract_asic_pcie_versions_and_validate(duthost) -> dict[str, str]:
    """
    Enumerate ASIC_PCIE* components and validate versions via `asic_pcie_fw_version`.
    """
    components = _extract_asic_pcie_components_from_facts(duthost)
    versions: dict[str, str] = {}
    for name, suffix in components:
        cmd = "asic_pcie_fw_version" if suffix is None else f"asic_pcie_fw_version {suffix}"
        r = _run(duthost, _with_timeout(cmd, 25))
        if r.rc != 0:
            pytest.fail(f"Failed to run `{cmd}`. rc={r.rc}\nstdout=\n{r.stdout}\nstderr=\n{r.stderr}")
        # `asic_pcie_fw_version` prints a single value.
        versions[name] = (r.stdout or "").strip()

    bad_versions = {"N/A", "UNKNOWN", "UNKNOWN_TIMEOUT", ""}
    invalid: list[tuple[str, str]] = [(n, v) for n, v in versions.items() if v in bad_versions]
    if invalid:
        pytest.fail(
            "ASIC PCIe component versions not readable via SDK path. "
            f"Invalid: {invalid}. Full parsed versions: {versions}"
        )

    return versions


def _asic_pcie_component_to_pddf_boot_flash_node(asic_pcie_component: str) -> str:
    """
    Map ASIC/SDK component key to the PDDF SPI
    subtree name used for creating the MTD device.

    Assuming:
    - ASIC_PCIE -> ASIC_BOOT_FLASH
    - ASIC_PCIE_D<idx> -> ASIC_BOOT_FLASH_D<idx>
    """
    return asic_pcie_component.replace("ASIC_PCIE", "ASIC_BOOT_FLASH", 1)


def _list_mtd_devices(duthost) -> set[str]:
    r = _run(duthost, "ls -1 /dev/mtd* 2>/dev/null || true")
    paths = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Keep /dev/mtdX and /dev/mtdXro
        if re.fullmatch(r"/dev/mtd\d+(ro)?", line):
            paths.add(line)
    return paths


def _create_pddf_subtree(duthost, node: str) -> None:
    r = _run(duthost, _with_timeout(f"sudo pddfparse.py --create-subtree {node}", 30))
    pytest_assert(r.rc == 0, f"Failed to create PDDF subtree {node}: {r.stdout}\n{r.stderr}")


def _delete_pddf_subtree(duthost, node: str) -> None:
    _run(duthost, _with_timeout(f"sudo pddfparse.py --delete-subtree {node}", 30))


def _read_512_bytes_sanity(duthost, mtd_path: str) -> None:
    # Use mtd_debug + hexdump on the DUT; parse hex output in test to detect all 0xff / all 0x00.
    # We require that at least the first 512 bytes from offset 0 are readable and non-empty.
    inner = (
        "set -e && "
        f"tmp=$(mktemp /tmp/asic_boot_flash.XXXXXX) && "
        "trap 'rm -f \"$tmp\"' EXIT && "
        f'sudo mtd_debug read {mtd_path} 0 512 "$tmp" >/dev/null 2>&1 && '
        'sudo hexdump -v -n 512 -e \'1/1 "%02x"\' "$tmp"'
    )
    cmd = f"bash -lc {shlex.quote(inner)}"
    r = _run(duthost, _with_timeout(cmd, 20))
    if r.rc != 0:
        pytest.fail(f"Failed to read 512 bytes from {mtd_path}. rc={r.rc}\nstdout=\n{r.stdout}\nstderr=\n{r.stderr}")
    hex_str = (r.stdout or "").strip()
    if len(hex_str) < 1024:
        pytest.fail(
            f"Flash read for {mtd_path} returned {len(hex_str) // 2} bytes (expected at least 512). "
            f"Output length: {len(hex_str)} hex chars."
        )
    hex_prefix = hex_str[:1024]
    all_ff = all(c in "fF" for c in hex_prefix)
    all_00 = all(c in "0" for c in hex_prefix)
    if all_ff or all_00:
        pytest.fail(
            f"Flash read for {mtd_path} returned all 0xff or all 0x00. "
            f"Expected programmed content (all_ff={all_ff} all_00={all_00})."
        )


def _assert_pddf_flash_readable(duthost, node: str) -> None:
    before = _list_mtd_devices(duthost)
    _create_pddf_subtree(duthost, node)
    try:
        after = _list_mtd_devices(duthost)
        new_mtd = sorted(after - before)
        pytest_assert(
            len(new_mtd) >= 1,
            f"PDDF subtree {node} created, but no new /dev/mtdX found. before={sorted(before)} after={sorted(after)}",
        )
        mtd_path = new_mtd[0]
        logger.info("Using MTD device for %s: %s", node, mtd_path)
        _read_512_bytes_sanity(duthost, mtd_path)
    finally:
        _delete_pddf_subtree(duthost, node)


def test_asic_pcie_access_paths(duthosts, enum_rand_one_per_hwsku_hostname):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    if duthost.facts.get("asic_type") != "broadcom":
        pytest.skip("Not a Broadcom ASIC platform")

    # 1) SDK path: extract ASIC_PCIE* component versions and validate.
    versions = _extract_asic_pcie_versions_and_validate(duthost)

    # 2) FPGA path: create node, read flash, delete node.
    for asic_pcie_component in versions.keys():
        node = _asic_pcie_component_to_pddf_boot_flash_node(asic_pcie_component)
        _assert_pddf_flash_readable(duthost, node)
