"""
Unit tests for send_with_retry / send_probe_burst in tests/packet_trimming/packet_trimming_helper.py

Run from the repository root:
    python3 -m pytest --noconftest tests/packet_trimming/unit_tests/ -v
"""
from unittest import mock

import pytest
from nnpy.errors import NNError

from tests.packet_trimming import packet_trimming_helper as helper

FILL = {"Ethernet0": "p0", "Ethernet8": "p8"}


def timeout():
    return NNError(110, "Connection timed out")


@pytest.fixture
def env(monkeypatch):
    """
    Mock the PTF send, the sleep and the module logger. Mocking the logger also keeps records out of
    pytest's log capture, whose format needs an attribute a conftest plugin adds (absent under
    --noconftest). helper.time is the global time module; monkeypatch restores sleep after each test,
    so keep this fixture function-scoped.
    """
    send = mock.Mock()
    monkeypatch.setattr(helper.testutils, "send", send)
    monkeypatch.setattr(helper.time, "sleep", mock.Mock())
    monkeypatch.setattr(helper, "logger", mock.Mock())
    adapter = mock.Mock()
    duthost = mock.Mock()
    duthost.get_asic_name.return_value = "th6"
    return send, adapter, duthost


def sent(send):
    return [c.kwargs["pkt"] for c in send.call_args_list]


def test_send_with_retry_runs_once_on_success(env):
    _, adapter, _ = env
    fn = mock.Mock()
    helper.send_with_retry(adapter, fn, "x")
    fn.assert_called_once_with()
    adapter.dataplane.flush.assert_not_called()


def test_send_with_retry_recovers_from_transient_timeouts(env):
    _, adapter, _ = env
    fn = mock.Mock(side_effect=[timeout(), timeout(), None])
    helper.send_with_retry(adapter, fn, "x")
    assert fn.call_count == 3
    assert adapter.dataplane.flush.call_count == 2
    assert helper.logger.warning.call_count == 2


def test_send_with_retry_gives_up_and_chains_the_last_timeout(env):
    _, adapter, _ = env
    fn = mock.Mock(side_effect=timeout())
    with pytest.raises(RuntimeError, match="after 3 attempts") as exc_info:
        helper.send_with_retry(adapter, fn, "x", max_retries=3)
    assert fn.call_count == 3
    assert isinstance(exc_info.value.__cause__, NNError)
    assert adapter.dataplane.flush.call_count == 2      # no wait or flush after the last attempt


def test_send_with_retry_does_not_retry_other_errors(env):
    _, adapter, _ = env
    fn = mock.Mock(side_effect=TypeError("bad port_id"))
    with pytest.raises(TypeError):
        helper.send_with_retry(adapter, fn, "x")
    fn.assert_called_once_with()
    helper.time.sleep.assert_not_called()


def test_probe_burst_tops_up_every_queue_then_sends_the_probe(env):
    send, adapter, duthost = env
    helper.send_probe_burst(duthost, adapter, 7, "probe", 100, FILL)
    assert sent(send) == ["p0", "p8", "probe"]


def test_failure_on_the_second_queue_restarts_the_whole_top_up(env):
    """Review item: the probe must follow a complete back-to-back fill, never a fill of p8 alone."""
    send, adapter, duthost = env
    send.side_effect = [None, timeout(), None, None, None]
    helper.send_probe_burst(duthost, adapter, 7, "probe", 100, FILL)
    assert sent(send) == ["p0", "p8", "p0", "p8", "probe"]


def test_failure_on_the_probe_redoes_the_top_up_first(env):
    send, adapter, duthost = env
    send.side_effect = [None, None, timeout(), None, None, None]
    helper.send_probe_burst(duthost, adapter, 7, "probe", 100, FILL)
    assert sent(send) == ["p0", "p8", "probe", "p0", "p8", "probe"]


def test_probe_burst_without_fill_sends_only_the_probe(env):
    send, adapter, duthost = env
    helper.send_probe_burst(duthost, adapter, 7, "probe", 100, None)
    assert sent(send) == ["probe"]


def test_probe_burst_skips_the_top_up_on_a_non_leaky_asic(env):
    send, adapter, duthost = env
    duthost.get_asic_name.return_value = "spc4"
    helper.send_probe_burst(duthost, adapter, 7, "probe", 100, FILL)
    assert sent(send) == ["probe"]


def test_send_with_retry_returns_the_attempts_made(env):
    _, adapter, _ = env
    fn = mock.Mock(side_effect=[timeout(), None])
    assert helper.send_with_retry(adapter, fn, "x") == 2


def test_probe_burst_counts_every_attempt_as_sent(env):
    send, adapter, duthost = env
    send.side_effect = [None, None, timeout(), None, None, None]
    sent_count = helper.send_probe_burst(duthost, adapter, 7, "probe", 100, FILL)
    assert sent_count == 2 * (helper.FILL_TOPUP_PACKET_COUNT * len(FILL) + 100)


def test_probe_burst_on_a_non_leaky_asic_counts_only_the_probe(env):
    send, adapter, duthost = env
    duthost.get_asic_name.return_value = "spc4"
    assert helper.send_probe_burst(duthost, adapter, 7, "probe", 100, FILL) == 100
