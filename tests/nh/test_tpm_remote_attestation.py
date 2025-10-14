import os
import shlex
import json
import logging
import pytest
import importlib.util
from cryptography import x509
from cryptography.x509.oid import NameOID

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology("any"),
]

logger = logging.getLogger(__name__)


def _get_nh_repo_root():
    """Find the parent 'nh' repository root by walking up from this test file.

    We intentionally avoid calling external commands. Starting from the directory
    containing this file, ascend directories until we reach '/' and return the first
    directory that contains an 'nhmfg' subdirectory (which indicates the nh repo root).
    Return None if not found so the caller can decide how to handle.
    """
    current_dir = os.path.abspath(os.path.dirname(__file__))

    while True:
        if os.path.isdir(os.path.join(current_dir, "nhmfg")):
            return current_dir
        parent = os.path.dirname(current_dir)
        if parent == current_dir:  # Reached filesystem root
            break
        current_dir = parent

    return None


def _detect_enrollment_env(nh_root: str, device_ip: str, user: str, password: str) -> bool:
    """Detect if DUT is enrolled in UAT or Production by reading IDevID CN via TPMDeviceVerifier.

    Returns: True if UAT False otherwise,
    Raises pytest.fail with context on errors.
    """
    # Resolve the absolute path to tpm_remote_attestation.py and import it explicitly via importlib
    att_path = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/tpm_remote_attestation.py")
    assert os.path.isfile(att_path) is True, f"tpm_remote_attestation.py not found at {att_path}"

    try:
        spec = importlib.util.spec_from_file_location("tpm_remote_attestation", att_path)
        if spec is None or spec.loader is None:
            pytest.fail(f"Failed to load module spec from {att_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        TPMDeviceVerifier = getattr(mod, "TPMDeviceVerifier")
    except Exception as e:
        pytest.fail(f"Failed to import TPMDeviceVerifier from path {att_path}: {e}")

    # Instantiate verifier locally and read the IDevID certificate pulled from device TPM NVRAM
    try:
        verifier = TPMDeviceVerifier(
            device=device_ip,
            ca_cert_path="unused",
            ssh_user=user,
            ssh_password=password,
        )
        pem = verifier.idevid_cert_pem
    except Exception as e:
        pytest.fail(f"Failed to retrieve IDevID certificate from device: {e}")

    assert pem is not None, "IDevID certificate not present/readable from TPM NVRAM"

    try:
        cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
        issuer = cert.issuer
        cn_attr = issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
        cn = cn_attr[0].value if cn_attr else ""
        cn_upper = cn.upper() if isinstance(cn, str) else str(cn).upper()
        logger.info(f"IDevID CN: {cn}")
        return "UAT" in cn_upper
    except Exception as e:
        pytest.fail(f"Failed parsing IDevID certificate to determine environment: {e}")


def _resolve_path(repo_root, rel_path):
    return os.path.join(repo_root, rel_path)


def _generate_golden_pcr_sets(nh_root, device_ip, user, password, output_dir, localhost):
    """Generate golden PCR sets from DUT via golden_pcr_generation.py.

    Args:
        nh_root: Path to the NH repository root
        device_ip: IP address of the device under test
        user: SSH username for device access
        password: SSH password for device access
        output_dir: Directory where golden PCR sets will be generated
        localhost: Ansible localhost object for command execution

    Returns:
        str: Path to the generated golden_pcr_sets.json file

    Raises:
        pytest.fail: If PCR generation fails or validation fails
    """
    # Resolve path to golden PCR generation script
    golden_gen_script = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/golden_pcr_generation.py")

    # Build the PCR generation command
    pcr_gen_cmd = (
        f"python3 {shlex.quote(golden_gen_script)} "
        f"--device {shlex.quote(device_ip)} "
        f"--user {shlex.quote(user)} "
        f"--password {shlex.quote(password)} "
        f"--output {shlex.quote(output_dir)} "
        f"--verbose"
    )

    # Execute the PCR generation command
    pcr_gen_res = localhost.command(pcr_gen_cmd, module_ignore_errors=True)
    if pcr_gen_res.get("rc", 1) != 0:
        stdout = pcr_gen_res.get("stdout", "").strip()
        stderr = pcr_gen_res.get("stderr", "").strip()
        pytest.fail(
            "Golden PCR generation failed (non-zero exit).\n"
            f"Command: {pcr_gen_cmd}\n"
            f"Exit code: {pcr_gen_res.get('rc')}\n"
            f"STDOUT:\n{stdout}\n"
            f"STDERR:\n{stderr}\n"
        )

    # Validate generated golden_pcr_sets.json
    pcr_set_file = os.path.join(output_dir, "golden_pcr_sets.json")
    assert os.path.isfile(pcr_set_file) is True, f"golden_pcr_sets.json was not created at {pcr_set_file}"

    try:
        with open(pcr_set_file, "r", encoding="utf-8") as f:
            file_content = f.read()
            data = json.loads(file_content)
        if not isinstance(data, dict) or "pcr_sets" not in data or not isinstance(data["pcr_sets"], dict):
            pytest.fail("golden_pcr_sets.json has unexpected format (missing 'pcr_sets' mapping)")
    except Exception as e:
        pytest.fail(f"Failed to read/parse generated golden_pcr_sets.json: {e}\nFile content:\n{file_content}")

    return pcr_set_file


def test_tpm_remote_attestation_succeeds(duthosts, enum_rand_one_per_hwsku_hostname, creds, localhost, tmp_path):
    """End-to-end TPM attestation: generate device-specific PCR sets then verify.

    Steps:
    - Ensure tpm2-tools present on controller (for sanity of environment)
    - Discover NH repo root (contains nhmfg)
    - Generate golden PCR sets from DUT via golden_pcr_generation.py into a temp dir
    - Run tpm_remote_attestation.py against DUT using generated PCR sets
    """

    # https://app.devrev.ai/nexthop/works/ISS-3526
    pytest.skip("Skipping this test until custom docker-sonic-mgmt is ready")

    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    if "x86_64-nexthop" not in duthost.facts["platform"]:
        pytest.skip("Skipping this test on non-NH platforms")

    nh_root = _get_nh_repo_root()
    assert nh_root is not None, (
        "Failed to locate NH repo root (directory containing 'nhmfg') by walking up from test file location."
    )

    # Paths to scripts and certificates (repo-relative with env override support)
    attest_script = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/tpm_remote_attestation.py")
    root_ca_uat = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/uat_assets/cryptohub_rootca.pem")
    root_ca_prod = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/prod_assets/cryptohub_rootca.pem")
    tpm_ca = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/tpm_certs/OptigaEccRootCA2.pem")
    tpm_intermediate = _resolve_path(nh_root, "nhmfg/scripts/tpm-attestation/tpm_certs/OptigaEccMfrCA070.pem")

    # Generate golden PCR sets into a unique temp subdir under pytest tmp_path
    gen_out_dir = os.path.join(str(tmp_path), "pcr_gen")
    os.makedirs(gen_out_dir, exist_ok=True)

    device_ip = duthost.mgmt_ip
    user = creds.get("sonicadmin_user", "admin")
    password = creds.get("sonicadmin_password")

    # Generate golden PCR sets using helper function
    pcr_set_file = _generate_golden_pcr_sets(nh_root, device_ip, user, password, gen_out_dir, localhost)

    # Detect environment (UAT vs PROD) by reading IDevID CN
    is_uat = _detect_enrollment_env(nh_root, device_ip, user, password)

    # Choose Root CA based on environment classification, allow overrides
    selected_root_ca = root_ca_uat if is_uat else root_ca_prod

    # Build attestation command; prefer python3 invocation to avoid exec bit assumptions
    attest_cmd = (
        f"python3 {shlex.quote(attest_script)} "
        f"--device {shlex.quote(device_ip)} "
        f"--user {shlex.quote(user)} "
        f"--password {shlex.quote(password)} "
        f"--root-ca-cert {shlex.quote(selected_root_ca)} "
        f"--tpm-ca {shlex.quote(tpm_ca)} "
        f"--tpm-intermediates {shlex.quote(tpm_intermediate)} "
        f"--pcr-sets {pcr_set_file} "
        f"--verbose "
    ).strip()

    attest_res = localhost.command(attest_cmd, module_ignore_errors=True)

    # On failure, include stdout/stderr for easier triage
    if attest_res.get("rc", 1) != 0:
        stdout = attest_res.get("stdout", "").strip()
        stderr = attest_res.get("stderr", "").strip()
        pytest.fail(
            "TPM remote attestation failed (non-zero exit).\n"
            f"Command: {attest_cmd}\n"
            f"Exit code: {attest_res.get('rc')}\n"
            f"STDOUT:\n{stdout}\n"
            f"STDERR:\n{stderr}\n"
        )

    # Success
    return
