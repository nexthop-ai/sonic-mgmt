"""
PTF-based gnmic client wrapper providing gNMI operations via the gnmic CLI.

This module provides a wrapper that invokes the gnmic binary on the PTF container
via ptfhost.shell(), hiding the CLI complexity behind clean, Pythonic interfaces.
"""
import json
import logging
import shlex
from typing import Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

# Connection-related keywords that indicate a connection failure rather than
# a generic gnmic command error.
_CONNECTION_KEYWORDS = (
    "connection refused",
    "no such host",
    "tls: ",
    "certificate",
    "handshake",
)

# Timeout-related keywords surfaced by gnmic/grpc when --timeout fires.
_TIMEOUT_KEYWORDS = (
    "context deadline exceeded",
    "operation timeout",
    "i/o timeout",
)


class PtfGnmicError(Exception):
    """Base exception for PtfGnmic operations."""
    pass


class GnmicConnectionError(PtfGnmicError):
    """Connection-related gnmic errors (target unreachable, TLS handshake failures)."""
    pass


class GnmicTimeoutError(PtfGnmicError):
    """gnmic operation timeout errors (--timeout exceeded)."""
    pass


class GnmicCallError(PtfGnmicError):
    """gnmic command execution errors (non-zero exit, malformed output)."""
    pass


class PtfGnmic:
    """
    PTF-based gnmic client wrapper.

    This class executes gnmic commands in the PTF container to interact with
    gNMI services on the DUT, providing process separation and a clean Python
    interface over the gnmic CLI.

    Usage follows the two-step initialization pattern established by PtfGrpc:
      1. Construct with target and mode: ``PtfGnmic(ptfhost, target)``
      2. Configure TLS certs: ``client.configure_tls_certificates(ca, cert, key)``
      3. Call methods: ``client.capabilities()``, ``client.get(paths)``
    """

    def __init__(self, ptfhost, target, plaintext=False):
        """
        Initialize PtfGnmic client.

        Args:
            ptfhost: PTF host instance for command execution
            target: Target string in host:port format (e.g. "10.0.0.1:50052")
            plaintext: If True, use --insecure flag instead of TLS certificates
        """
        self.ptfhost = ptfhost
        self.target = str(target)
        self.plaintext = plaintext
        self.ca_cert = None
        self.client_cert = None
        self.client_key = None
        self.timeout = 10  # seconds; matches gnmic's own --timeout default
        self._gnmic_path = "/usr/local/bin/gnmic"
        logger.info(f"Initialized PtfGnmic: target={self.target}, plaintext={self.plaintext}")

    def configure_timeout(self, timeout_seconds: int) -> None:
        """
        Configure the gnmic per-operation timeout.

        Args:
            timeout_seconds: Timeout in seconds (passed to gnmic as ``--timeout Ns``).
        """
        self.timeout = int(timeout_seconds)
        logger.debug(f"Configured gnmic timeout: {self.timeout}s")

    def configure_tls_certificates(self, ca_cert: str, client_cert: str, client_key: str) -> None:
        """
        Configure TLS certificates for mutual authentication.

        Args:
            ca_cert: Path to CA certificate file on the PTF container
            client_cert: Path to client certificate file on the PTF container
            client_key: Path to client private key file on the PTF container
        """
        self.ca_cert = ca_cert
        self.client_cert = client_cert
        self.client_key = client_key
        self.plaintext = False
        logger.info(f"Configured TLS certificates: ca={ca_cert}, cert={client_cert}, key={client_key}")

    def _build_base_cmd(self) -> str:
        """Build the gnmic invocation prefix with target, timeout, and TLS/insecure flags."""
        cmd = f"{self._gnmic_path} -a {self.target} --timeout {self.timeout}s"
        if self.plaintext:
            cmd += " --insecure"
        elif self.ca_cert and self.client_cert and self.client_key:
            cmd += (
                f" --tls-ca {self.ca_cert}"
                f" --tls-cert {self.client_cert}"
                f" --tls-key {self.client_key}"
            )
        return cmd

    def _run(self, cmd: str, op_name: str) -> str:
        """Execute a gnmic command on the PTF host and return stdout, or raise."""
        logger.debug(f"Executing gnmic command: {cmd}")
        result = self.ptfhost.shell(cmd, module_ignore_errors=True)

        rc = result["rc"]
        stdout = result.get("stdout", "").strip()
        stderr = result.get("stderr", "").strip()

        if rc != 0:
            stderr_lower = stderr.lower()
            if any(kw in stderr_lower for kw in _TIMEOUT_KEYWORDS):
                raise GnmicTimeoutError(
                    f"gnmic {op_name} timed out after {self.timeout}s: {stderr}"
                )
            if any(kw in stderr_lower for kw in _CONNECTION_KEYWORDS):
                raise GnmicConnectionError(
                    f"gnmic connection failed to {self.target}: {stderr}"
                )
            raise GnmicCallError(
                f"gnmic {op_name} failed (rc={rc}): {stderr}"
            )
        return stdout

    def capabilities(self) -> Dict:
        """
        Query gNMI capabilities from the target device.

        Executes ``gnmic capabilities --format json`` on the PTF container and
        returns the parsed JSON output.

        Returns:
            Dictionary containing gNMI capabilities:
            - supported-encodings: List of supported encoding strings
            - supported-models: List of model dicts (name, organization, version)
            - gnmi-version: gNMI protocol version string

        Raises:
            GnmicConnectionError: If connection to target fails (refused, TLS errors)
            GnmicCallError: If gnmic exits with non-zero code or returns invalid JSON
        """
        cmd = f"{self._build_base_cmd()} capabilities --format json"
        stdout = self._run(cmd, "capabilities")
        try:
            return json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as e:
            raise GnmicCallError(
                f"gnmic returned invalid JSON: {e}\nOutput: {stdout}"
            )

    def get(
        self,
        paths: Union[str, Iterable[str]],
        *,
        datatype: str = "ALL",
        encoding: str = "json_ietf",
        prefix: Optional[str] = None,
    ) -> List[Dict]:
        """
        Issue a gNMI Get RPC against the target device.

        Executes ``gnmic get --path <p> [--path <p> ...] --format json`` on
        the PTF container and returns the parsed JSON output.

        Args:
            paths: A single gNMI path string or an iterable of path strings,
                e.g. "/openconfig-interfaces:interfaces/interface[name=Ethernet0]/state".
            datatype: gNMI data type filter. One of
                "ALL", "CONFIG", "STATE", "OPERATIONAL". Defaults to "ALL".
            encoding: Encoding to request (e.g. "json_ietf", "proto").
                Defaults to "json_ietf".
            prefix: Optional gNMI prefix applied to all paths.

        Returns:
            List of per-source response dicts as emitted by gnmic. Each entry
            contains "source", "timestamp", and an "updates" list of
            {"Path": ..., "values": ...} items.

        Raises:
            GnmicCallError: If no path is provided, datatype is invalid,
                gnmic returns non-zero, or stdout is not valid JSON.
            GnmicConnectionError: If the target is unreachable or TLS fails.
        """
        if isinstance(paths, str):
            path_list: List[str] = [paths]
        else:
            path_list = list(paths)
        if not path_list:
            raise GnmicCallError("gnmic get requires at least one path")

        valid_types = {"ALL", "CONFIG", "STATE", "OPERATIONAL"}
        if datatype.upper() not in valid_types:
            raise GnmicCallError(
                f"invalid datatype {datatype!r}, expected one of {sorted(valid_types)}"
            )

        cmd = f"{self._build_base_cmd()} get"
        if prefix is not None:
            cmd += f" --prefix {shlex.quote(prefix)}"
        for p in path_list:
            cmd += f" --path {shlex.quote(p)}"
        cmd += f" --type {datatype.upper()}"
        cmd += f" --encoding {shlex.quote(encoding)}"
        cmd += " --format json"

        stdout = self._run(cmd, "get")
        try:
            return json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as e:
            raise GnmicCallError(
                f"gnmic returned invalid JSON: {e}\nOutput: {stdout}"
            )

    def __str__(self):
        return f"PtfGnmic(target={self.target}, plaintext={self.plaintext})"

    def __repr__(self):
        return self.__str__()
