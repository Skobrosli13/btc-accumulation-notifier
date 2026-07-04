"""§8 severity tiers: ACT/RISK/FAIL raise ntfy push priority; default otherwise."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app import notify
from tests.factories import make_config


def _cfg():
    return make_config(ntfy_topic="t", ntfy_server="https://ntfy.example")


def _priority_sent(severity):
    with patch("app.notify.requests.post") as post:
        post.return_value = MagicMock(raise_for_status=lambda: None)
        assert notify._send_ntfy(_cfg(), "title", "body", severity=severity) is True
        return post.call_args.kwargs["headers"]["Priority"]


def test_fail_is_urgent():
    assert _priority_sent("FAIL") == "urgent"


def test_act_and_risk_are_high():
    assert _priority_sent("ACT") == "high"
    assert _priority_sent("RISK") == "high"


def test_unsevere_default():
    assert _priority_sent(None) == "default"
    assert _priority_sent("WHATEVER") == "default"


def test_send_threads_severity_through():
    with patch("app.notify._send_ntfy") as ntfy:
        ntfy.return_value = True
        assert notify.send(_cfg(), "t", "b", severity="FAIL") is True
        assert ntfy.call_args.kwargs.get("severity") == "FAIL"
