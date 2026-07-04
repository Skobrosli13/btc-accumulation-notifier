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


# --- quiet hours (§4: nothing but FAIL rings at 3am) -----------------------

def _at(hour):
    from datetime import datetime, timezone
    return datetime(2026, 7, 1, hour, 30, tzinfo=timezone.utc)


def test_quiet_window_membership():
    assert notify._in_quiet_hours("03-11", _at(5)) is True
    assert notify._in_quiet_hours("03-11", _at(12)) is False
    assert notify._in_quiet_hours("22-06", _at(23)) is True    # wraps midnight
    assert notify._in_quiet_hours("22-06", _at(3)) is True
    assert notify._in_quiet_hours("22-06", _at(12)) is False
    assert notify._in_quiet_hours("", _at(5)) is False          # disabled
    assert notify._in_quiet_hours("garbage", _at(5)) is False


def test_quiet_hours_mute_all_but_fail(monkeypatch):
    monkeypatch.setattr(notify, "_in_quiet_hours", lambda w, now=None: True)
    cfg = _cfg()
    assert notify._push_priority(cfg, "ACT") == "min"
    assert notify._push_priority(cfg, "RISK") == "min"
    assert notify._push_priority(cfg, None) == "min"
    assert notify._push_priority(cfg, "FAIL") == "urgent"   # dead-man always rings


def test_outside_quiet_hours_full_priority(monkeypatch):
    monkeypatch.setattr(notify, "_in_quiet_hours", lambda w, now=None: False)
    assert notify._push_priority(_cfg(), "ACT") == "high"
    assert notify._push_priority(_cfg(), None) == "default"
