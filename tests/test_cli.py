"""``hermes ntfy-approval setup|test`` without Hermes: the handlers get a settings loader."""

from __future__ import annotations

import argparse

from conftest import cli, fake_only, run_async, transport


def _args(*argv):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    return parser.parse_args(list(argv))


def _unset():
    raise transport.ConfigError("NTFY_APPROVAL_TOPIC is not set")


def test_setup_prints_a_fresh_private_topic_and_the_commands(capsys):
    assert cli.run(_args("setup"), _unset, load_server=lambda: "https://ntfy.example.com") == 0
    out = capsys.readouterr().out
    topic = next(line.strip() for line in out.splitlines() if line.strip().startswith("hermes-"))
    assert len(topic) == 39 and transport.make_settings(topic=topic).topic == topic
    assert "subscribe to this topic on https://ntfy.example.com" in out
    assert f"hermes config set NTFY_APPROVAL_TOPIC {topic}" in out
    assert "hermes config set security.approval.transport ntfy" in out
    assert "hermes config set security.approval.transport builtin" in out


def test_setup_makes_a_new_topic_each_time(capsys):
    cli.run(_args("setup"), _unset)
    cli.run(_args("setup"), _unset)
    topics = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("hermes-")]
    assert len(topics) == 2 and topics[0] != topics[1]


def test_setup_leaves_a_working_setup_alone_unless_asked(capsys):
    configured = transport.make_settings(topic="hermes-" + "k" * 30)
    assert cli.run(_args("setup"), lambda: configured) == 0
    out = capsys.readouterr().out
    assert "Already set up: topic hermes-kkk…" in out and "k" * 30 not in out
    assert cli.run(_args("setup", "--new-topic"), lambda: configured) == 0
    assert "hermes config set NTFY_APPROVAL_TOPIC hermes-" in capsys.readouterr().out


def test_setup_reports_a_bad_server_setting(capsys):
    def bad_server():
        raise transport.ConfigError("server 'ftp://x' must be an http(s) URL")

    assert cli.run(_args("setup"), _unset, load_server=bad_server) == 1
    assert "Fix the server setting first" in capsys.readouterr().out


def test_test_reports_the_tapped_button(server_url, topic, phone, capsys):
    settings = transport.make_settings(server=server_url, topic=topic)
    done = run_async(cli.run, _args("test", "--timeout", "20"), lambda: settings, "work")
    note = phone.notification()
    assert note["title"] == "Hermes needs your approval (work)"
    assert "nothing will run" in note["message"]
    phone.tap(note, "Approve for session")
    assert done.result(timeout=30) == 0
    assert "Your phone answered: Approve for session." in capsys.readouterr().out


def test_test_without_a_tap_fails(server_url, topic, capsys):
    settings = transport.make_settings(server=server_url, topic=topic)
    assert cli.run(_args("test", "--timeout", "1"), lambda: settings) == 1  # clamped up to 10 s
    assert "No answer within 10 s" in capsys.readouterr().out


def test_test_without_setup_fails(capsys):
    assert cli.run(_args("test"), _unset) == 1
    assert "Not set up: NTFY_APPROVAL_TOPIC is not set" in capsys.readouterr().out


@fake_only
def test_test_explains_an_access_problem(capsys):
    from fake_ntfy import FakeNtfy

    guarded = FakeNtfy(token="tk_right").start()
    try:
        settings = transport.make_settings(server=guarded.url, topic="g" * 20, token="tk_wrong")
        assert cli.run(_args("test", "--timeout", "10"), lambda: settings) == 1
        out = capsys.readouterr().out
        assert "HTTP 401" in out and "NTFY_APPROVAL_TOKEN" in out
        assert "tk_wrong" not in out
    finally:
        guarded.stop()


def test_an_unreachable_server_is_reported(capsys):
    settings = transport.make_settings(server="http://127.0.0.1:9", topic="u" * 20)
    assert cli.run(_args("test", "--timeout", "10"), lambda: settings) == 1
    assert "Could not reach http://127.0.0.1:9" in capsys.readouterr().out


def test_no_action_prints_usage(capsys):
    assert cli.run(_args(), _unset) == 1
    assert "usage: hermes ntfy-approval {setup,test}" in capsys.readouterr().out


def test_sample_request_is_shaped_like_hermes_request():
    request = cli.SampleRequest(request_id="a" * 32, command="c", description="d", timeout_seconds=5)
    assert transport.offered_choices(request) == ("once", "session", "deny")
    assert request.respond("deny") == "deny"
