"""Settings validation, the notification the phone gets, and how replies are matched."""

from __future__ import annotations

import pytest

from conftest import Request, transport

make = transport.make_settings
LONG = "a" * 16


def test_defaults():
    settings = make(topic="hermes-" + "x" * 20)
    assert settings.server == "https://ntfy.sh"
    assert settings.priority == 4 and settings.send_command is True and settings.token == ""
    assert settings.reply_topic == "hermes-" + "x" * 20 + "-reply"


@pytest.mark.parametrize("raw, expected", [
    ("https://ntfy.example.com/", "https://ntfy.example.com"),
    ("ntfy.example.com", "https://ntfy.example.com"),
    ("http://10.0.0.5:8080", "http://10.0.0.5:8080"),
    ("https://example.com/ntfy/", "https://example.com/ntfy"),
    ("HTTPS://NTFY.SH", "https://ntfy.sh"),
    (None, "https://ntfy.sh"),
    ("", "https://ntfy.sh"),
])
def test_server_is_normalized(raw, expected):
    assert make(server=raw, topic=LONG).server == expected


@pytest.mark.parametrize("raw", [
    "https://user:pass@ntfy.sh", "https://ntfy.sh?auth=abc", "https://ntfy.sh/#x", "ftp://ntfy.sh",
    "https://", "https://ntfy.sh:99999",
])
def test_bad_servers_are_refused(raw):
    with pytest.raises(transport.ConfigError):
        make(server=raw, topic=LONG)


def test_credentials_in_the_server_url_point_to_the_token_setting():
    with pytest.raises(transport.ConfigError, match="NTFY_APPROVAL_TOKEN"):
        make(server="https://me:secret@ntfy.example.com", topic=LONG)


@pytest.mark.parametrize("topic", ["", "   ", "has space", "slash/topic", "ünicode-topic-long", "a" * 59])
def test_bad_topics_are_refused(topic):
    with pytest.raises(transport.ConfigError):
        make(topic=topic, token="tk_x")


def test_the_longest_topic_leaves_room_for_the_reply_suffix():
    assert len(make(topic="a" * 58).reply_topic) == 64


def test_a_short_topic_needs_a_token():
    with pytest.raises(transport.ConfigError, match="at least 16"):
        make(topic="a" * 15)
    assert make(topic="approvals", token="tk_abc").topic == "approvals"


@pytest.mark.parametrize("token", ["tk abc", "tk\nabc", "tk\x00"])
def test_tokens_with_header_breaking_characters_are_refused(token):
    with pytest.raises(transport.ConfigError):
        make(topic=LONG, token=token)


def test_priority_and_send_command():
    assert make(topic=LONG, priority="URGENT").priority == 5
    assert make(topic=LONG, priority="min").priority == 1
    with pytest.raises(transport.ConfigError):
        make(topic=LONG, priority="loud")
    with pytest.raises(transport.ConfigError):
        make(topic=LONG, send_command="no")
    assert make(topic=LONG, send_command=False).send_command is False


def _note(request=None, **settings):
    request = request or Request()
    s = make(topic=LONG, **settings)
    tokens = {choice: f"tok-{choice}-0123456789abcdef" for choice in transport.offered_choices(request)}
    return request, s, tokens, transport.build_notification(request, s, tokens, profile="work")


def test_notification_carries_three_buttons_and_never_always():
    request, s, tokens, note = _note()
    assert [a["label"] for a in note["actions"]] == ["Approve once", "Approve for session", "Deny"]
    assert "always" not in tokens
    for action, choice in zip(note["actions"], ["once", "session", "deny"]):
        assert action == {"action": "http", "label": action["label"], "url": f"https://ntfy.sh/{LONG}-reply",
                          "method": "POST", "clear": True,
                          "body": f"hermes-approval v1 {request.request_id} {choice} {tokens[choice]}"}
        assert "headers" not in action  # the token is never embedded in a notification
    assert note["topic"] == LONG and note["priority"] == 4 and note["tags"] == ["warning"]
    assert note["title"] == "Hermes needs your approval (work)"


def test_the_access_token_never_goes_into_a_notification():
    _request, _s, _t, note = _note(token="tk_secret_value")
    assert "tk_secret_value" not in str(note)
    assert all("headers" not in action for action in note["actions"])


def test_a_once_only_request_gets_no_session_button():
    _request, _s, tokens, note = _note(Request(allowed_choices=("once", "deny")))
    assert [a["label"] for a in note["actions"]] == ["Approve once", "Deny"]
    assert set(tokens) == {"once", "deny"}


def test_message_text():
    request, _s, _t, note = _note(Request(command="rm -rf /srv/data", description="recursive delete",
                                          timeout_seconds=300))
    assert note["message"] == "recursive delete\n\nrm -rf /srv/data\n\nAnswer within 5 min. No answer means deny."
    _r, _s, _t, short = _note(Request(timeout_seconds=45))
    assert short["message"].endswith("Answer within 45 s. No answer means deny.")


def test_send_command_off_sends_only_the_reason():
    _r, _s, _t, note = _note(Request(command="curl https://secret.example/path"), send_command=False)
    assert "secret.example" not in note["message"]
    assert note["message"].startswith("recursive delete\n\n")


def test_default_profile_has_a_plain_title():
    request = Request()
    s = make(topic=LONG)
    note = transport.build_notification(request, s, {"once": "x" * 32, "deny": "y" * 32}, profile="default")
    assert note["title"] == "Hermes needs your approval"


def test_long_text_is_clipped_on_utf8_boundaries_below_ntfys_4096_byte_limit():
    _r, _s, _t, note = _note(Request(command="ş" * 5000, description="ğ" * 1000))
    assert len(note["message"].encode("utf-8")) < 3000
    note["message"].encode("utf-8").decode("utf-8")  # no broken character at a cut
    assert "…" in note["message"]


def test_match_reply():
    request = Request()
    tokens = {"once": "A" * 32, "session": "B" * 32, "deny": "C" * 32}
    rid = request.request_id
    assert transport.match_reply(f"hermes-approval v1 {rid} once {'A' * 32}", rid, tokens) == "once"
    assert transport.match_reply(f"hermes-approval v1 {rid} deny {'C' * 32}\n", rid, tokens) == "deny"
    for text in [
        f"hermes-approval v1 {rid} once {'B' * 32}",       # another button's token
        f"hermes-approval v1 {rid} always {'A' * 32}",     # a choice that was never offered
        f"hermes-approval v1 {'0' * 32} once {'A' * 32}",  # another request
        f"hermes-approval v2 {rid} once {'A' * 32}",
        f"hermes-approval v1 {rid}  once {'A' * 32}",
        f"hermes-approval v1 {rid} once {'A' * 31}",
        f"hermes-approval v1 {rid} once",
        "", None, 42,
    ]:
        assert transport.match_reply(text, rid, tokens) is None, text


def test_secrets_come_from_hermes_profile_scope_when_it_exists(monkeypatch):
    import sys
    import types

    from conftest import plugin

    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: {"NTFY_APPROVAL_TOPIC": " scoped-topic "}.get(name, default)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setenv("NTFY_APPROVAL_TOPIC", "process-env-topic")
    assert plugin.read_secret("NTFY_APPROVAL_TOPIC") == "scoped-topic"
    assert plugin.read_secret("NTFY_APPROVAL_TOKEN") == ""

    def refuse(name, default=None):
        raise RuntimeError("no profile secret scope while multiplexing")

    scope.get_secret = refuse
    with pytest.raises(RuntimeError):  # fail closed; the transport turns this into a denial
        plugin.read_secret("NTFY_APPROVAL_TOPIC")


def test_secrets_fall_back_to_the_environment_without_hermes(monkeypatch):
    import sys

    from conftest import plugin

    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # makes the import fail
    monkeypatch.setenv("NTFY_APPROVAL_TOPIC", " env-topic ")
    assert plugin.read_secret("NTFY_APPROVAL_TOPIC") == "env-topic"
