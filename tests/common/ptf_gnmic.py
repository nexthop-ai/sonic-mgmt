"""
PTF-based gnmic client wrapper providing gNMI operations via the gnmic CLI.

This module provides a wrapper that invokes the gnmic binary on the PTF container
via ptfhost.shell(), hiding the CLI complexity behind clean, Pythonic interfaces.
"""
import json
import logging
import shlex
from enum import StrEnum
from typing import Iterable

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


class SubscribeMode(StrEnum):
    STREAM = "stream"
    POLL = "poll"
    ONCE = "once"


class StreamMode(StrEnum):
    SAMPLE = "sample"
    ON_CHANGE = "on_change"
    TARGET_DEFINED = "target_defined"


def _normalize_paths(paths: str | Iterable[str]) -> list[str]:
    if isinstance(paths, str):
        return [paths]
    return list(paths)


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

    def _run_stream_for(self, cmd: str, collect_seconds: int, op_name: str) -> str:
        """
        Execute a long-running gnmic subscribe command for a bounded time window.

        Uses GNU ``timeout`` to stop collection after ``collect_seconds`` and
        still returns whatever gnmic emitted before being signalled.
        """
        if collect_seconds <= 0:
            raise GnmicCallError("collect_seconds must be > 0")

        wrapped_cmd = (
            f"timeout --signal=INT {int(collect_seconds)}s "
            f"sh -c {shlex.quote(cmd)}"
        )
        logger.debug("Executing bounded gnmic stream command: %s", wrapped_cmd)
        result = self.ptfhost.shell(wrapped_cmd, module_ignore_errors=True)

        rc = result["rc"]
        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()

        # timeout exits with 124; SIGINT may surface as 130 depending on shell/tooling.
        if rc not in (0, 124, 130):
            stderr_lower = stderr.lower()
            if any(kw in stderr_lower for kw in _TIMEOUT_KEYWORDS):
                raise GnmicTimeoutError(
                    f"gnmic {op_name} timed out after {collect_seconds}s: {stderr}"
                )
            if any(kw in stderr_lower for kw in _CONNECTION_KEYWORDS):
                raise GnmicConnectionError(
                    f"gnmic connection failed to {self.target}: {stderr}"
                )
            raise GnmicCallError(f"gnmic {op_name} failed (rc={rc}): {stderr}")

        if not stdout:
            raise GnmicCallError(
                f"gnmic {op_name} produced no stdout in {collect_seconds}s; stderr: {stderr}"
            )

        return stdout

    def _parse_json_sequence(self, stdout: str) -> list[dict]:
        """
        Parse one or more JSON objects concatenated in stdout.

        gnmic subscribe emits multiple JSON objects separated by whitespace,
        for example a notification object followed by {"sync-response": true},
        and may append a non-JSON shutdown trailer such as
        ``received signal 'interrupt'. terminating...`` when stopped via SIGINT.
        """
        decoder = json.JSONDecoder()
        objects: list[dict] = []
        idx = 0
        length = len(stdout)

        while idx < length:
            while idx < length and stdout[idx].isspace():
                idx += 1
            if idx >= length:
                break

            try:
                obj, idx = decoder.raw_decode(stdout, idx)
            except ValueError as e:
                tail = stdout[idx:].strip()
                if tail.startswith("received signal"):
                    logger.debug("Ignoring gnmic shutdown trailer: %s", tail)
                    break
                raise GnmicCallError(
                    f"gnmic returned invalid JSON stream: {e}\nOutput: {stdout}"
                )
            objects.append(obj)

        return objects

    def capabilities(self) -> dict:
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
        paths: str | Iterable[str],
        *,
        datatype: str = "ALL",
        encoding: str = "json_ietf",
        prefix: str | None = None,
    ) -> list[dict]:
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
        path_list = _normalize_paths(paths)
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

    def _normalize_mode(self, mode: SubscribeMode | str) -> SubscribeMode:
        try:
            return SubscribeMode(mode)
        except ValueError as e:
            raise GnmicCallError(f"invalid subscribe mode: {mode}") from e

    def _normalize_stream_mode(self, stream_mode: StreamMode | str) -> StreamMode:
        try:
            return StreamMode(stream_mode)
        except ValueError as e:
            raise GnmicCallError(f"invalid subscribe stream_mode: {stream_mode}") from e

    def _validate_subscribe_args(
        self,
        *,
        mode: SubscribeMode,
        stream_mode: StreamMode,
        sample_interval: str | None,
    ) -> None:
        if mode is not SubscribeMode.STREAM:
            raise GnmicCallError(
                f"only {SubscribeMode.STREAM} is supported in this PR, got: {mode}"
            )
        if stream_mode is not StreamMode.SAMPLE:
            raise GnmicCallError(
                f"only {StreamMode.SAMPLE} stream_mode is supported in this PR, got: {stream_mode}"
            )
        if sample_interval is None:
            raise GnmicCallError(
                "sample_interval is required for SAMPLE subscriptions"
            )

    def subscribe(
        self,
        paths: str | Iterable[str],
        *,
        mode: SubscribeMode | str = SubscribeMode.STREAM,
        stream_mode: StreamMode | str = StreamMode.SAMPLE,
        sample_interval: str | None = None,
        encoding: str = "json_ietf",
        prefix: str | None = None,
        target: str | None = None,
        collect_seconds: int = 6,
        heartbeat_interval: str | None = None,
        suppress_redundant: bool = False,
        updates_only: bool = False,
        extra_args: Iterable[str] | None = None,
    ) -> list[dict]:
        """
        Execute a bounded gNMI subscribe via gnmic and return notification objects.

        Initial implementation is intentionally narrow:
        - only mode=``SubscribeMode.STREAM``
        - only stream_mode=``StreamMode.SAMPLE``

        Args:
            paths: A single gNMI path string or an iterable of path strings.
            mode: gNMI subscription mode. Only ``SubscribeMode.STREAM`` is
                supported here.
            stream_mode: STREAM sub-mode. Only ``StreamMode.SAMPLE`` is
                supported here.
            sample_interval: Server-side sampling interval as a Go duration
                (e.g. "1s", "500ms"). Required for sample subscriptions.
            encoding: Encoding to request. Defaults to ``json_ietf``.
            prefix: Optional gNMI prefix applied to all paths.
            target: Optional gNMI target (e.g. "OC-YANG", "COUNTERS_DB",
                "OTHERS"). Required by SONiC gNMI for non-OpenConfig paths
                to route to the correct data tree.
            collect_seconds: Wall-clock bound on the streaming RPC, in seconds.
            heartbeat_interval: Optional heartbeat interval passed through
                to gnmic.
            suppress_redundant: Whether to pass ``--suppress-redundant``.
            updates_only: Whether to pass ``--updates-only``.
            extra_args: Optional sequence of tokens appended to the gnmic
                subscribe command, each individually shell-quoted, e.g.
                ``["--qos", "32"]``. Escape hatch for gnmic flags not
                modelled as explicit parameters. See
                https://gnmic.openconfig.net/cmd/subscribe/ for the full
                set. A plain string is rejected to avoid the
                str-is-iterable pitfall.

        Returns:
            List of notification dicts (those carrying ``updates``), in
            receive order. Control markers such as ``{"sync-response": true}``
            are filtered out.

        Raises:
            GnmicCallError: If validation fails, gnmic exits abnormally,
                output is not valid JSON, or no notifications were received.
            GnmicConnectionError: If the target is unreachable or TLS fails.
            GnmicTimeoutError: If gnmic surfaces a deadline/timeout error.
        """
        path_list = _normalize_paths(paths)
        normalized_mode = self._normalize_mode(mode)
        normalized_stream_mode = self._normalize_stream_mode(stream_mode)

        if not path_list:
            raise GnmicCallError("subscribe requires at least one path")

        self._validate_subscribe_args(
            mode=normalized_mode,
            stream_mode=normalized_stream_mode,
            sample_interval=sample_interval,
        )

        cmd = self._build_base_cmd()
        cmd += " subscribe --format json"
        cmd += f" --mode {shlex.quote(normalized_mode.value)}"
        cmd += f" --stream-mode {shlex.quote(normalized_stream_mode.value)}"
        cmd += f" --sample-interval {shlex.quote(sample_interval)}"
        cmd += f" --encoding {shlex.quote(encoding)}"

        if prefix:
            cmd += f" --prefix {shlex.quote(prefix)}"
        if target:
            cmd += f" --target {shlex.quote(target)}"

        for path in path_list:
            cmd += f" --path {shlex.quote(path)}"

        if heartbeat_interval:
            cmd += f" --heartbeat-interval {shlex.quote(heartbeat_interval)}"
        if suppress_redundant:
            cmd += " --suppress-redundant"
        if updates_only:
            cmd += " --updates-only"

        if extra_args is not None:
            if isinstance(extra_args, str):
                raise GnmicCallError(
                    "extra_args must be a sequence of tokens "
                    "(e.g. ['--qos', '32']), not a single string"
                )
            for token in extra_args:
                cmd += f" {shlex.quote(token)}"

        stdout = self._run_stream_for(cmd, collect_seconds, "subscribe")
        objects = self._parse_json_sequence(stdout)

        notifications = [
            obj for obj in objects
            if isinstance(obj, dict) and "updates" in obj
        ]
        if not notifications:
            raise GnmicCallError(
                f"gnmic subscribe returned no notification objects.\nOutput: {stdout}"
            )
        return notifications

    def __str__(self):
        return f"PtfGnmic(target={self.target}, plaintext={self.plaintext})"

    def __repr__(self):
        return self.__str__()
