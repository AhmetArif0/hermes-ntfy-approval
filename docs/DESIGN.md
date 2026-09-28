# ntfy-approval: design

## Goal

Let someone who stepped away from the terminal answer Hermes' dangerous-command prompts from a
phone, with a free, widely used push app and without exposing a port on the Hermes machine.

## Facts this design relies on (verified 2026-09-28)

Hermes (main `35272ce28b` and v0.21.5 / `v2026.9.24`):

1. `ctx.register_approval_transport(name, present)` registers a transport per profile's
   PluginManager; it is used only when `security.approval.transport` names it. A selected
   transport replaces every built-in prompt surface (`tools/approval_prompt.py`).
2. The host builds an immutable `ApprovalRequest` whose `command` and `description` went through
   `redact_sensitive_text(..., force=True)`, with `request_id`, `digest`, `timeout_seconds`
   (`approvals.timeout`, default 300) and `allowed_choices` (`once`, `session` if allowed,
   `always` if allowed, `deny`).
3. `present` runs on a bounded daemon worker (8 slots). The host waits until its own deadline and
   then denies with `timeout`; a late result is discarded. An exception, a non-`ApprovalDecision`,
   a mismatched id/digest or a choice not offered all deny. `request.respond(choice)` is the only
   accepted answer.
4. The worker is a bare `threading.Thread`: it does not inherit the caller's `contextvars`, so
   under a multi-profile gateway it sees the launch profile's `HERMES_HOME` and no secret scope
   (open fix: hermes-agent #114580). `RegisteredApprovalTransport.profile_home` is the home at
   registration, and the host only uses a transport whose home matches the active one.
5. Transports are consulted only when a human can answer: interactive CLI/TUI/ACP, gateway, and
   `HERMES_EXEC_ASK`. Cron, `-q` and unattended platforms resolve from `approvals.*_mode` first.
6. `config_schema` with `type: secret` + `env:` keeps secrets in `.env`; `hermes config set
   UPPER_SNAKE value` writes `.env`. A secret field marked `required: true` makes the loader warn
   on every load (it looks for the value in `config.yaml`), so the topic is validated by the
   plugin instead.
7. `agent.secret_scope.get_secret` is the credential path the plugin-storage docs point to: it
   reads the profile scope, falls back to `os.environ` without multiplexing, and raises when a
   multiplexing process has no scope.

ntfy (server v2.28.0, built from source and run locally; docs):

8. `POST /` with JSON publishes; unknown fields are ignored; the response carries `id` and `time`.
9. Up to three action buttons. An `http` action makes the app send the request itself, with the
   given method, headers and body; `clear: true` dismisses the notification after a successful
   request. Supported on Android, iOS and the web app per the docs.
10. `GET /<topic>/json` sends an `open` event first, then cached messages from `since`, then live
    messages and keepalives (45 s default). `since=<unix time>` is inclusive; `since=<message id>`
    excludes that message.
11. Without an explicit sequence id, a message's sequence id is its id, and
    `DELETE /<topic>/<id>` publishes `message_delete`, which removes the notification from
    subscribed devices (server 2.16+). A DELETE for an unknown id still returns 200.
12. With `auth-default-access: deny-all`: no credentials give 403, a wrong token 401, and
    `ntfy access everyone <topic> write-only` allows anonymous writes but not reads.

## Flow

1. Validate settings (server URL, topic, token, priority) at call time; fail closed on any error.
2. Draw one random code (`secrets.token_urlsafe(24)`) per offered button: `once`, `session`,
   `deny`. `always` is never offered.
3. Open the reply-topic stream and wait for `open`, so an answer can't slip in before we listen.
4. Publish the notification. Each button is an `http` POST to `<server>/<topic>-reply` with body
   `hermes-approval v1 <request_id> <choice> <code>`.
5. Read the reply topic. A message counts only if the id matches and the code matches the code
   of the choice it names (`hmac.compare_digest`). Anything else is ignored.
6. On a dropped stream, reconnect with `since` = the last reply-topic message id (or the publish
   time), so an answer sent during the gap is not lost. Fatal statuses (400/401/403/404) raise at
   once; others retry every 2 s. Read timeouts shrink as the deadline approaches, so the
   worker never outlives it.
7. Delete the notification (best effort), then return `request.respond(choice)` or raise
   `TimeoutError` at the deadline.

## Decisions

- **Reply through ntfy, not a local HTTP endpoint.** A callback URL would need the phone to reach
  the Hermes machine (port forwarding, a tunnel, TLS). Posting to a second topic on the same
  server needs nothing on the Hermes side.
- **Separate reply topic.** Answers on the request topic would show up as notifications.
- **Per-button codes instead of one code per request.** Seeing someone's "Deny" answer on the reply
  topic does not let anyone turn it into an approval.
- **No token in notifications.** The recommended ACL makes the reply topic anonymous write-only,
  so the button needs no credentials, and an access token never ends up in the server's message
  cache or on devices.
- **Topic length floor without a token.** On a public server the topic name is the secret.
- **Refuse on a profile mismatch** (fact 4) instead of reading another profile's settings.
- **Standard library only.** No dependency to pin, and the client is small enough to read in
  review. Redirects are refused so a token can't follow one to another host.
- **Setup prints commands and writes nothing.** `.env` and `config.yaml` stay under Hermes' own
  `hermes config set`, which knows where secrets go.

## Rejected

- Sending the `Authorization` header in the button: it would put the token into every
  notification.
- Offering `always`: a fourth button doesn't fit, and permanent allowlisting from a lock screen is
  too easy to do by accident.
- Polling (`poll=1`) instead of a stream: on ntfy.sh a 300 s wait at a useful interval runs into
  the request rate limit.
- Answering `deny` ourselves at the timeout: it would be recorded as the user's explicit refusal.
  Letting the host time out keeps silence and refusal distinct.

## Verification

- Unit tests run on an in-process ntfy server (`tests/fake_ntfy.py`) with fault switches (dropped
  streams, refused reconnects, quiet streams, redirects, tokens).
- The same tests run against a real ntfy server (`NTFY_TEST_SERVER`), plus the self-hosting
  recipe (`NTFY_TEST_AUTH_SERVER`: deny-all, token, write-only reply topic).
- Integration tests load the plugin through Hermes' PluginManager and drive the real gate
  (`check_all_command_guards`) with nothing patched, real `hermes` CLI subprocesses, and real
  `AIAgent` turns against Hermes' `FakeLLMServer`, on v0.21.5 and main.
