"""The transport against an ntfy server (FakeNtfy, or a real one with NTFY_TEST_SERVER)."""

from __future__ import annotations

import os
import time

import pytest

from conftest import Phone, Request, client_mod, fake_only, run_async, transport
from fake_ntfy import FakeNtfy


def _settings(server_url, topic, **extra):
    return transport.make_settings(server=server_url, topic=topic, **extra)


@pytest.mark.parametrize("label, choice", [("Approve once", "once"), ("Approve for session", "session"),
                                           ("Deny", "deny")])
def test_each_button_returns_its_decision(server_url, topic, phone, label, choice):
    request = Request()
    result = run_async(transport.present, request, _settings(server_url, topic))
    phone.tap(phone.notification(), label)
    assert result.result(timeout=15) == ("decision", request.request_id, request.digest, choice)


def test_the_answered_notification_is_withdrawn_from_every_device(server_url, topic, phone):
    result = run_async(transport.present, Request(), _settings(server_url, topic))
    note = phone.notification()
    phone.tap(note, "Deny")
    result.result(timeout=15)
    deletes = [e for e in phone.events() if e["event"] == "message_delete"]
    assert [e["sequence_id"] for e in deletes] == [note["id"]]


def test_forged_and_foreign_replies_are_ignored(server_url, topic, phone):
    request = Request()
    result = run_async(transport.present, request, _settings(server_url, topic))
    note = phone.notification()
    reply_topic = topic + "-reply"
    once = next(a["body"] for a in note["actions"] if a["label"] == "Approve once")
    prefix, version, rid, _choice, token = once.split(" ")
    for text in [
        f"{prefix} {version} {rid} always {token}",       # upgrade to a choice never offered
        f"{prefix} {version} {rid} session {token}",      # reuse the once token for another button
        f"{prefix} {version} {'0' * 32} once {token}",    # right token, another request
        f"{prefix} {version} {rid} once {'x' * len(token)}",
        "approve",
    ]:
        phone.post(reply_topic, text)
    time.sleep(0.5)
    assert not result.done()
    phone.tap(note, "Deny")
    assert result.result(timeout=15)[3] == "deny"


def test_two_requests_in_flight_get_their_own_answers(server_url, topic, phone):
    first, second = Request(), Request()
    settings = _settings(server_url, topic)
    a = run_async(transport.present, first, settings)
    note_a = phone.notification()
    b = run_async(transport.present, second, settings)
    note_b = phone.notification()
    by_id = {n["actions"][0]["body"].split(" ")[2]: n for n in (note_a, note_b)}
    phone.tap(by_id[second.request_id], "Deny")
    assert b.result(timeout=15)[1:] == (second.request_id, second.digest, "deny")
    assert not a.done()
    phone.tap(by_id[first.request_id], "Approve once")
    assert a.result(timeout=15)[1:] == (first.request_id, first.digest, "once")


def test_silence_times_out_and_withdraws_the_notification(server_url, topic, phone):
    request = Request(timeout_seconds=2)
    started = time.monotonic()
    result = run_async(transport.present, request, _settings(server_url, topic))
    note = phone.notification()
    with pytest.raises(TimeoutError):
        result.result(timeout=15)
    elapsed = time.monotonic() - started
    assert 1.8 <= elapsed < 4.5, elapsed
    assert any(e["event"] == "message_delete" and e["sequence_id"] == note["id"] for e in phone.events())


def test_a_tap_after_the_timeout_is_harmless(server_url, topic, phone):
    result = run_async(transport.present, Request(timeout_seconds=1.5), _settings(server_url, topic))
    note = phone.notification()
    with pytest.raises(TimeoutError):
        result.result(timeout=15)
    phone.tap(note, "Approve once")  # lands on the reply topic; nobody is waiting for it
    later = Request(timeout_seconds=1.5)
    again = run_async(transport.present, later, _settings(server_url, topic))
    phone.notification()
    with pytest.raises(TimeoutError):
        again.result(timeout=15)


def test_an_unreachable_server_fails_fast():
    started = time.monotonic()
    with pytest.raises(OSError):
        transport.present(Request(timeout_seconds=30), _settings("http://127.0.0.1:9", "t" * 20))
    assert time.monotonic() - started < 10


@fake_only
def test_a_dropped_stream_is_resumed_without_losing_the_answer(fake, topic):
    phone = Phone(fake.url, topic)
    request = Request(timeout_seconds=20)
    result = run_async(transport.present, request, _settings(fake.url, topic))
    note = phone.notification()
    fake.refuse_streams_until = time.time() + 1.0  # the reconnect is refused for a second
    fake.drop_streams()
    time.sleep(0.2)
    phone.tap(note, "Approve for session")  # answered while nobody is listening
    assert result.result(timeout=15)[3] == "session"
    streams = [path for method, path, _h in fake.requests if method == "GET" and "-reply/json" in path]
    assert streams[0].endswith("/json") and "since=" in streams[-1]


@fake_only
def test_an_answer_sent_before_the_open_event_still_counts(fake, topic):
    """Some servers or proxies may replay cached messages ahead of ntfy's "open" event."""
    fake.open_after_backlog = True
    phone = Phone(fake.url, topic)
    result = run_async(transport.present, Request(timeout_seconds=20), _settings(fake.url, topic))
    note = phone.notification()
    fake.refuse_streams_until = time.time() + 1.0
    fake.drop_streams()
    time.sleep(0.2)
    phone.tap(note, "Deny")  # replayed on reconnect, before "open"
    assert result.result(timeout=15)[3] == "deny"


@fake_only
def test_the_stream_never_outlives_the_deadline():
    quiet = FakeNtfy(keepalive=60).start()  # no keepalive inside the test window
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            transport.present(Request(timeout_seconds=1.5), _settings(quiet.url, "q" * 20))
        assert time.monotonic() - started < 3.5
    finally:
        quiet.stop()


@fake_only
def test_keepalives_do_not_stretch_the_deadline():
    chatty = FakeNtfy(keepalive=0.3).start()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            transport.present(Request(timeout_seconds=2), _settings(chatty.url, "k" * 20))
        assert time.monotonic() - started < 3.5
    finally:
        chatty.stop()


@fake_only
def test_the_token_is_sent_and_a_wrong_token_fails_at_once():
    guarded = FakeNtfy(token="tk_right").start()
    try:
        topic = "g" * 20
        phone = Phone(guarded.url, topic, token="tk_right")
        request = Request()
        result = run_async(transport.present, request, _settings(guarded.url, topic, token="tk_right"))
        phone.tap(phone.notification(), "Approve once")  # anonymous write to the reply topic
        assert result.result(timeout=15)[3] == "once"
        sent = {h.get("Authorization") for m, p, h in guarded.requests if topic in p or p == "/"}
        assert "Bearer tk_right" in sent
        assert all("tk_right" not in str(e) for e in guarded.events)  # never inside a notification

        started = time.monotonic()
        with pytest.raises(client_mod.NtfyError) as info:
            transport.present(Request(timeout_seconds=30), _settings(guarded.url, topic, token="tk_wrong"))
        assert info.value.status == 401 and info.value.fatal
        assert time.monotonic() - started < 5
    finally:
        guarded.stop()


@fake_only
def test_redirects_are_not_followed_so_the_token_stays_on_the_server():
    elsewhere = FakeNtfy().start()
    redirecting = FakeNtfy(redirect_to=elsewhere.url).start()
    try:
        with pytest.raises(client_mod.NtfyError, match="redirect"):
            transport.present(Request(), _settings(redirecting.url, "r" * 20, token="tk_secret"))
        assert elsewhere.requests == []
    finally:
        redirecting.stop()
        elsewhere.stop()


def test_no_choice_to_show_is_a_config_error(server_url, topic):
    with pytest.raises(transport.ConfigError):
        transport.present(Request(allowed_choices=("always",)), _settings(server_url, topic))


AUTH_SERVER = os.environ.get("NTFY_TEST_AUTH_SERVER", "").rstrip("/")


@pytest.mark.skipif(not AUTH_SERVER, reason="needs NTFY_TEST_AUTH_SERVER: a real server with access control")
def test_a_server_with_access_control_and_a_write_only_reply_topic():
    """The README's self-hosting recipe: deny-all by default, the token owns the topic, and
    everyone may only write to the reply topic, which is where the phone's button POSTs."""
    token, topic = os.environ["NTFY_TEST_AUTH_TOKEN"], os.environ["NTFY_TEST_AUTH_TOPIC"]
    phone = Phone(AUTH_SERVER, topic, token=token)
    request = Request()
    result = run_async(transport.present, request, _settings(AUTH_SERVER, topic, token=token))
    phone.tap(phone.notification(request.request_id), "Approve for session")  # the tap carries no credentials
    assert result.result(timeout=15)[3] == "session"
    with pytest.raises(client_mod.NtfyError) as missing:
        transport.present(Request(), _settings(AUTH_SERVER, "x" * 20))
    assert missing.value.status == 403 and missing.value.fatal


@fake_only
def test_a_long_lived_stream_is_cut_short_near_the_deadline(monkeypatch):
    """A stream opened with a long read timeout must not keep the worker past the deadline when
    the next keepalive would only come after it."""
    monkeypatch.setattr(transport, "READ_TIMEOUT", 3.0)
    slow = FakeNtfy(keepalive=2.5).start()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            transport.present(Request(timeout_seconds=4), _settings(slow.url, "s" * 20))
        assert time.monotonic() - started < 4.6
    finally:
        slow.stop()


def test_oversized_responses_are_refused():
    with pytest.raises(client_mod.NtfyError, match="oversized"):
        client_mod._parse_message(b"{" + b" " * client_mod.MAX_RESPONSE_BYTES + b"}")
