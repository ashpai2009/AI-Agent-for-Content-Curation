"""The Claude Code CLI provider, exercised against a **fake executable**.

Nothing here contacts Claude. Each test writes a small shell script into `tmp_path`, points
`COUNCIL_CLAUDE_CLI_PATH` at it, and asserts on what the real subprocess machinery does
with what that script produces — so the argument list, stdin handling, environment
filtering, timeout behaviour and envelope parsing are all tested for real rather than
mocked away.

The assertions worth reading twice are the ones about what must *never* happen: the payload
never appearing in the argument list, no credential variable reaching the child, and no
session flag being passed.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from oatutor_council.config import ConfigurationError, Settings, load_settings
from oatutor_council.llm.base import (
    AgentRole,
    LLMRequest,
    ProviderConfigurationError,
    ProviderError,
    ProviderOutputTooLarge,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUsageLimited,
    RetryingClient,
    call_structured,
)
from oatutor_council.llm.claude_cli import (
    FORBIDDEN_CHILD_VARIABLES,
    ClaudeCLIClient,
    build_command,
    child_environment,
    classify_cli_failure,
    describe_authentication,
    extract_structured_output,
    extract_usage,
    find_reset_time,
    require_authentication,
)


class Reply(BaseModel):
    verdict: str


def settings(tmp_path: Path, executable: str, **overrides) -> Settings:
    defaults = dict(
        claude_cli_path=executable,
        claude_model="sonnet",
        claude_effort="medium",
        data_root=tmp_path,
        max_repair_attempts=3,
        max_validation_rounds=2,
        step_budget=100,
        llm_call_budget=100,
        interrupted_retry_budget=2,
        max_concurrent_jobs=1,
        max_upload_bytes=1024,
        lease_seconds=60,
        provider_max_attempts=1,
        provider_timeout_seconds=10.0,
    )
    return Settings(**{**defaults, **overrides})


def request(**overrides) -> LLMRequest:
    base = dict(
        role=AgentRole.INITIAL_AUDITOR,
        system_prompt="you are the auditor",
        user_payload="the workbook block goes here",
        schema=Reply.model_json_schema(),
        job_id="job-1",
    )
    return LLMRequest(**{**base, **overrides})


def fake_cli(tmp_path: Path, script: str) -> str:
    """Write an executable stand-in for `claude` and return its path."""
    path = tmp_path / "fake-claude"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(path)


#: Records what it was given, then answers. `"$@"` is written verbatim so a test can assert
#: on the exact argument list the adapter built.
RECORDING_SCRIPT = """
printf '%s\\n' "$@" > "$RECORD_ARGS"
cat > "$RECORD_STDIN"
env > "$RECORD_ENV"
cat <<'JSON'
{"type":"result","subtype":"success","is_error":false,
 "result":"{\\"verdict\\": \\"accept\\"}",
 "model":"claude-sonnet-x","duration_ms":1234,
 "usage":{"input_tokens":100,"output_tokens":20,
          "cache_creation_input_tokens":7,"cache_read_input_tokens":9}}
JSON
"""


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    """A fake CLI plus the files it records into."""
    args = tmp_path / "args.txt"
    stdin = tmp_path / "stdin.txt"
    environment = tmp_path / "env.txt"
    monkeypatch.setenv("RECORD_ARGS", str(args))
    monkeypatch.setenv("RECORD_STDIN", str(stdin))
    monkeypatch.setenv("RECORD_ENV", str(environment))

    executable = fake_cli(tmp_path, RECORDING_SCRIPT)
    # The recorder needs its own paths, which the production allowlist correctly drops --
    # so this fixture widens the allowlist for the duration of the test only.
    monkeypatch.setattr(
        "oatutor_council.llm.claude_cli._ENVIRONMENT_ALLOWLIST",
        ("HOME", "PATH", "RECORD_ARGS", "RECORD_STDIN", "RECORD_ENV"),
    )
    return executable, args, stdin, environment


# --------------------------------------------------------------------------------------
# The invocation
# --------------------------------------------------------------------------------------


def test_a_successful_call_returns_the_structured_output(tmp_path, recorder):
    executable, *_ = recorder
    client = ClaudeCLIClient(settings(tmp_path, executable))
    answer = call_structured(client, request(), Reply)
    assert answer.verdict == "accept"


def test_the_argument_list_is_the_one_that_was_verified(tmp_path, recorder):
    """Each of these was checked against `claude --help` at 2.1.219. A flag that does not
    exist aborts every call, and a flag that is missing changes what the model can do."""
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    sent = args.read_text().splitlines()

    assert "--print" in sent
    assert sent[sent.index("--output-format") + 1] == "json"
    assert sent[sent.index("--model") + 1] == "sonnet"
    assert sent[sent.index("--effort") + 1] == "medium"
    assert sent[sent.index("--permission-mode") + 1] == "dontAsk"
    assert sent[sent.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    for flag in ("--safe-mode", "--disable-slash-commands", "--strict-mcp-config",
                 "--no-session-persistence"):
        assert flag in sent, flag


def test_the_call_carries_the_configured_turn_ceiling(tmp_path, recorder):
    """`--max-turns` was left out of this list once, on the evidence that the CLI's
    abbreviated `--help` does not mention it -- when the binary carries both the flag and
    its description and the published reference documents it. `--tools ""` already leaves
    nothing to iterate on, but a limit the CLI enforces is worth more than a limit that
    follows from an argument about what the model should have no reason to do.

    The value is **2**. At 1 the live pilot lost four independent-review calls to
    `Reached maximum number of turns (1)` before the structured output arrived, and retry
    recovered every one -- so the ceiling meant to bound spend was buying extra calls."""
    executable, args, _, _ = recorder
    configured = settings(tmp_path, executable)
    assert configured.claude_max_turns == 2

    ClaudeCLIClient(configured).complete(request())
    sent = args.read_text().splitlines()

    assert sent[sent.index("--max-turns") + 1] == str(configured.claude_max_turns)


def test_the_turn_ceiling_is_a_setting_rather_than_a_literal(tmp_path, recorder):
    """It is a fuse: tightenable in an incident without a redeploy."""
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable, claude_max_turns=1)).complete(request())
    sent = args.read_text().splitlines()

    assert sent[sent.index("--max-turns") + 1] == "1"


def test_role_effort_overrides_are_loaded_and_sent(tmp_path, recorder, monkeypatch):
    """A documented quality control must be reachable from the service environment,
    not only by constructing ``Settings`` inside a test."""
    executable, args, _, _ = recorder
    monkeypatch.setenv("COUNCIL_CLAUDE_CLI_PATH", executable)
    monkeypatch.setenv("COUNCIL_CLAUDE_EFFORT", "medium")
    monkeypatch.setenv("COUNCIL_INITIAL_AUDITOR_EFFORT", "high")
    configured = load_settings(env_file=None)

    ClaudeCLIClient(configured).complete(request(role=AgentRole.INITIAL_AUDITOR))
    sent = args.read_text().splitlines()

    assert configured.effort_for(AgentRole.WRITER.value) == "medium"
    assert configured.effort_for(AgentRole.INITIAL_AUDITOR.value) == "high"
    assert sent[sent.index("--effort") + 1] == "high"


def test_invalid_role_effort_is_a_startup_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError, match="COUNCIL_WRITER_EFFORT"):
        settings(tmp_path, "claude", role_effort={"writer": "extreme"})


def test_a_turn_ceiling_below_one_is_refused(tmp_path):
    """Zero turns cannot produce a response, so it is a configuration error rather than a
    very tight budget."""
    with pytest.raises(ConfigurationError, match="at least 1"):
        settings(tmp_path, "claude", claude_max_turns=0)


def test_tools_are_disabled_with_an_explicit_empty_argument(tmp_path, recorder):
    """The empty string is load-bearing. Omitting it enables every built-in tool, which
    would turn an agent that reads one block into one that can read the filesystem."""
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    sent = args.read_text().splitlines()

    assert sent[sent.index("--tools") + 1] == ""


def test_the_agents_system_prompt_replaces_the_default(tmp_path, recorder):
    """`--system-prompt`, not `--append-system-prompt`: appending would leave Claude
    Code's own coding-assistant preamble underneath the agent's instructions as a second,
    invisible instruction set."""
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    sent = args.read_text().splitlines()

    assert sent[sent.index("--system-prompt") + 1] == "you are the auditor"
    assert "--append-system-prompt" not in sent


def test_the_json_schema_is_transmitted_exactly(tmp_path, recorder):
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    sent = args.read_text().splitlines()

    assert json.loads(sent[sent.index("--json-schema") + 1]) == Reply.model_json_schema()


def test_the_workbook_reaches_the_cli_only_on_stdin(tmp_path, recorder):
    """Arguments are visible in `ps` to every user on the machine, and the payload is a
    curator's workbook. It is never an argument, never an environment variable, and never
    a temporary file."""
    executable, args, stdin, environment = recorder
    payload = "PROBLEM angles1 SECRET-CURATOR-CONTENT"
    ClaudeCLIClient(settings(tmp_path, executable)).complete(
        request(user_payload=payload)
    )

    assert stdin.read_text().strip() == payload
    assert "SECRET-CURATOR-CONTENT" not in args.read_text()
    assert "SECRET-CURATOR-CONTENT" not in environment.read_text()


def test_no_session_is_continued_resumed_or_persisted(tmp_path, recorder):
    """Context isolation is structural here: no session means nothing can carry an
    agent's reasoning into another agent's call."""
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    sent = args.read_text()

    for flag in ("--continue", "--resume", "--fork-session", "--session-id", "--bare"):
        assert flag not in sent, flag
    assert "--no-session-persistence" in sent


def test_dangerous_permission_flags_are_never_passed(tmp_path, recorder):
    executable, args, _, _ = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    sent = args.read_text()

    assert "--dangerously-skip-permissions" not in sent
    assert "--allow-dangerously-skip-permissions" not in sent


# --------------------------------------------------------------------------------------
# The child environment
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("secret", FORBIDDEN_CHILD_VARIABLES)
def test_no_credential_variable_reaches_the_child(secret):
    """An inherited `ANTHROPIC_API_KEY` would silently switch the CLI from the user's
    subscription to metered API billing. A developer with one exported in their shell must
    not be able to cause that by starting the service."""
    environment = child_environment({**os.environ, secret: "leaked-value"})
    assert secret not in environment
    assert "leaked-value" not in environment.values()


def test_the_child_environment_is_an_allowlist_not_a_denylist():
    """A denylist is a promise to have thought of every variable that matters."""
    environment = child_environment(
        {"HOME": "/home/x", "PATH": "/bin", "SOME_INTERNAL_SECRET": "s", "AWS_SECRET": "s"}
    )
    assert set(environment) == {"HOME", "PATH"}


def test_the_child_keeps_what_the_cli_needs_to_find_its_own_login():
    """`HOME` is how the CLI reaches its keychain-backed credentials. Dropping it would
    make every call fail as unauthenticated."""
    environment = child_environment({"HOME": "/home/x", "PATH": "/bin"})
    assert environment["HOME"] == "/home/x"


def test_the_real_environment_does_not_leak_the_session_that_launched_us(tmp_path, recorder):
    """This service may well be started from a Claude Code session. None of that session's
    state should reach a child that is meant to be a fresh, isolated call."""
    executable, _, _, environment = recorder
    ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    seen = environment.read_text()

    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
        assert f"\n{name}=" not in f"\n{seen}", name


# --------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------


def test_a_missing_executable_is_a_configuration_error(tmp_path):
    client = ClaudeCLIClient(settings(tmp_path, "/nonexistent/claude"))
    with pytest.raises(ProviderConfigurationError) as error:
        client.complete(request())
    assert error.value.retryable is False
    assert error.value.status == "executable_missing"


def test_a_nonzero_exit_becomes_a_provider_error(tmp_path):
    executable = fake_cli(tmp_path, "echo 'something went wrong' >&2\nexit 4\n")
    client = ClaudeCLIClient(settings(tmp_path, executable))
    with pytest.raises(ProviderError) as error:
        client.complete(request())
    assert "something went wrong" in str(error.value)


def test_a_logged_out_cli_is_a_configuration_error_naming_the_fix(tmp_path):
    executable = fake_cli(
        tmp_path, "echo 'Not logged in. Please run claude auth login.' >&2\nexit 1\n"
    )
    client = ClaudeCLIClient(settings(tmp_path, executable))
    with pytest.raises(ProviderConfigurationError) as error:
        client.complete(request())
    assert "claude auth login" in str(error.value)
    assert error.value.retryable is False


def test_a_subscription_usage_limit_is_not_retried(tmp_path):
    """A Pro allowance behaves like the plan quota the previous provider taught us to
    separate from a burst: retrying does not help and costs real time.

    Asserted by counting the processes that actually started, under a retry wrapper set
    to four attempts -- `retryable is False` alone only says what the exception carries."""
    counter = tmp_path / "invocations.txt"
    executable = fake_cli(
        tmp_path,
        f"echo x >> {counter}\n"
        "echo \"You've reached your usage limit. Your limit will reset at 3pm.\" >&2\n"
        "exit 1\n",
    )
    client = RetryingClient(
        ClaudeCLIClient(settings(tmp_path, executable)),
        attempts=4,
        backoff_ceiling=0,
        sleep=lambda _: None,
    )
    with pytest.raises(ProviderUsageLimited) as error:
        client.complete(request())
    assert error.value.retryable is False
    assert error.value.resets_at is not None
    assert counter.read_text().count("x") == 1


def test_one_complete_starts_exactly_one_process(tmp_path):
    """The contract that makes the audit trail and the model-call budget honest.

    Retrying used to live in here, where four `claude` processes could produce one
    recorded row and one budget charge. It now lives above the recorder, so this client
    must start one process and let the failure out."""
    counter = tmp_path / "invocations.txt"
    executable = fake_cli(tmp_path, f"echo x >> {counter}\necho '503 overloaded' >&2\nexit 1\n")

    with pytest.raises(ProviderUnavailable):
        ClaudeCLIClient(
            settings(tmp_path, executable, provider_max_attempts=4)
        ).complete(request())

    assert counter.read_text().count("x") == 1, "the provider retried on its own"


def test_a_transient_failure_is_retried_by_the_layer_above(tmp_path):
    """The other half: moving retry out must not lose it."""
    counter = tmp_path / "invocations.txt"
    executable = fake_cli(
        tmp_path,
        f"echo x >> {counter}\n"
        f"if [ $(wc -l < {counter}) -lt 3 ]; then echo '503 overloaded' >&2; exit 1; fi\n"
        'printf \'{"type":"result","subtype":"success","result":'
        '"{\\\\"verdict\\\\": \\\\"accept\\\\"}"}\\n\'\n',
    )
    client = RetryingClient(
        ClaudeCLIClient(settings(tmp_path, executable)),
        attempts=4,
        backoff_ceiling=0,
        sleep=lambda _: None,
    )

    assert call_structured(client, request(), Reply).verdict == "accept"
    assert counter.read_text().count("x") == 3


# --------------------------------------------------------------------------------------
# The output cap
# --------------------------------------------------------------------------------------


def test_a_runaway_response_is_stopped_while_it_is_being_read(tmp_path, monkeypatch):
    """The cap has to bound memory, not just what is kept.

    The first version sliced `communicate()`'s return value, which caps what is retained
    and bounds nothing: the whole response had already been buffered before the slice
    ran. A child that never stops talking must be cut off mid-stream."""
    monkeypatch.setattr("oatutor_council.llm.claude_cli.MAX_OUTPUT_BYTES", 4096)
    finished = tmp_path / "it-ran-to-completion.txt"
    executable = fake_cli(
        tmp_path,
        # Never stops, unless somebody stops it.
        "while true; do head -c 65536 /dev/zero | tr '\\0' 'a'; done\n"
        f"echo done > {finished}\n",
    )

    with pytest.raises(ProviderOutputTooLarge) as error:
        ClaudeCLIClient(
            settings(tmp_path, executable, provider_timeout_seconds=20.0)
        ).complete(request())

    assert error.value.retryable is False, "the same prompt produces the same runaway"
    assert not finished.exists(), "the child was allowed to finish"


def test_a_runaway_takes_the_whole_process_group_with_it(tmp_path, monkeypatch):
    """Same argument as the timeout path: an orphaned helper keeps talking to the
    provider after we have stopped listening."""
    monkeypatch.setattr("oatutor_council.llm.claude_cli.MAX_OUTPUT_BYTES", 4096)
    marker = tmp_path / "orphan-was-alive.txt"
    executable = fake_cli(
        tmp_path,
        f"( sleep 3; echo alive > {marker} ) &\n"
        "while true; do head -c 65536 /dev/zero | tr '\\0' 'a'; done\n",
    )

    with pytest.raises(ProviderOutputTooLarge):
        ClaudeCLIClient(
            settings(tmp_path, executable, provider_timeout_seconds=20.0)
        ).complete(request())

    time.sleep(3.5)
    assert not marker.exists(), "a grandchild survived the output cap"


def test_output_just_under_the_cap_is_returned_intact(tmp_path, monkeypatch):
    """The cap must not truncate an ordinary response, and a large one is ordinary."""
    monkeypatch.setattr("oatutor_council.llm.claude_cli.MAX_OUTPUT_BYTES", 1_000_000)
    padding = "b" * 200_000
    executable = fake_cli(
        tmp_path,
        'printf \'{"type":"result","subtype":"success","result":'
        f'"{{\\\\"verdict\\\\": \\\\"{padding}\\\\"}}"}}\\n\'\n',
    )

    answer = call_structured(
        ClaudeCLIClient(settings(tmp_path, executable)), request(), Reply
    )
    assert answer.verdict == padding


def test_a_usage_limit_with_no_stated_reset_does_not_invent_one(tmp_path):
    """A job that wakes on a guessed timestamp spends a call to rediscover it is still
    limited. Absent means absent."""
    executable = fake_cli(tmp_path, "echo 'You have reached your weekly limit.' >&2\nexit 1\n")
    with pytest.raises(ProviderUsageLimited) as error:
        ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    assert error.value.resets_at is None


def test_a_timeout_raises_and_kills_the_whole_process_group(tmp_path):
    """`subprocess.run(timeout=...)` kills only the direct child. The CLI spawns helpers,
    and an orphan keeps talking to the provider after we have stopped listening."""
    marker = tmp_path / "orphan-was-alive.txt"
    executable = fake_cli(
        tmp_path,
        # A child of the fake CLI that outlives it unless the group is killed.
        f"( sleep 5; echo alive > {marker} ) &\n"
        "sleep 5\n",
    )
    client = ClaudeCLIClient(settings(tmp_path, executable, provider_timeout_seconds=0.5))

    with pytest.raises(ProviderTimeout):
        client.complete(request())

    # Give the orphan longer than it asked for; if the group died it never writes.
    time.sleep(1.5)
    assert not marker.exists(), "a grandchild survived the timeout"


def test_a_malformed_envelope_is_a_provider_failure_not_a_schema_error(tmp_path):
    """Blaming the model for a response the CLI never produced sends a debugger to the
    wrong layer."""
    executable = fake_cli(tmp_path, "echo 'this is not json'\n")
    with pytest.raises(ProviderError) as error:
        ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    assert error.value.status == "malformed_envelope"


def test_an_envelope_reporting_failure_with_a_zero_exit_is_still_a_failure(tmp_path):
    """The exit code and the envelope disagree exactly where it matters most."""
    executable = fake_cli(
        tmp_path,
        "cat <<'JSON'\n"
        '{"type":"result","subtype":"error_during_execution","is_error":true,'
        '"result":"the model was overloaded"}\n'
        "JSON\n",
    )
    with pytest.raises(ProviderError) as error:
        ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    assert "overloaded" in str(error.value)


def test_output_with_no_structured_result_fails_loudly(tmp_path):
    executable = fake_cli(
        tmp_path, "cat <<'JSON'\n{\"type\":\"result\",\"subtype\":\"success\"}\nJSON\n"
    )
    with pytest.raises(ProviderError) as error:
        ClaudeCLIClient(settings(tmp_path, executable)).complete(request())
    assert error.value.status == "no_structured_output"


# --------------------------------------------------------------------------------------
# Envelope parsing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        {"structured_output": {"verdict": "accept"}},
        {"structuredOutput": {"verdict": "accept"}},
        {"result": {"verdict": "accept"}},
        {"result": '{"verdict": "accept"}'},
    ],
)
def test_structured_output_is_found_wherever_the_cli_puts_it(envelope):
    """The envelope shape is unverified until the smoke test runs, so the parser checks
    each plausible location and fails loudly rather than guessing."""
    assert json.loads(extract_structured_output(envelope)) == {"verdict": "accept"}


def test_usage_carries_cache_tokens_through():
    """Part of any apparent saving comes from the provider's own caching rather than from
    batching, and a number nobody records cannot answer which."""
    usage = extract_usage(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 8,
            },
            "duration_ms": 900,
        },
        elapsed_ms=1000,
    )
    assert usage["cache_read_tokens"] == 8
    assert usage["cache_creation_tokens"] == 2
    assert usage["duration_ms"] == 900


def test_no_dollar_cost_is_ever_derived():
    """The CLI reports a cost estimate for API-key users. On a subscription it is fiction,
    and fiction in a curator's report is worse than an absent number."""
    usage = extract_usage({"total_cost_usd": 0.42, "usage": {}}, elapsed_ms=1)
    assert not any("cost" in key or "usd" in key for key in usage)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("overloaded, try later", ProviderUnavailable),
        ("503 service unavailable", ProviderUnavailable),
        ("blocked by safety policy", ProviderRefused),
        ("unknown model 'sonnet-9'", ProviderConfigurationError),
        ("something nobody has seen", ProviderError),
    ],
)
def test_cli_failures_are_classified_into_one_class(text, expected):
    assert isinstance(classify_cli_failure(1, "", text), expected)


@pytest.mark.parametrize(
    "text",
    [
        "your limit will reset at 3:00pm",
        '{"resets_at": "2026-08-12T15:00:00Z"}',
        "try again after tomorrow at noon",
    ],
)
def test_a_stated_reset_time_is_preserved(text):
    assert find_reset_time(text) is not None


# --------------------------------------------------------------------------------------
# Startup authentication
# --------------------------------------------------------------------------------------


def test_a_subscription_login_is_recognised():
    described = describe_authentication(
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"}
    )
    assert described == {
        "authenticated": True,
        "subscription_login": True,
        "subscription_type": "pro",
    }


def test_an_api_credential_is_not_a_subscription_login():
    """This service is configured to run on a subscription and never on metered billing,
    so an API-key login is refused rather than quietly accepted."""
    described = describe_authentication(
        {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"}
    )
    assert described["authenticated"] is True
    assert described["subscription_login"] is False


def test_describing_authentication_never_reveals_the_account():
    described = describe_authentication(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "email": "someone@example.com",
            "orgId": "9ffa926f",
            "orgName": "Someone's Organization",
            "subscriptionType": "pro",
        }
    )
    serialised = json.dumps(described)
    assert "example.com" not in serialised
    assert "9ffa926f" not in serialised
    assert "Organization" not in serialised


def test_startup_refuses_a_logged_out_cli(tmp_path, monkeypatch):
    executable = fake_cli(tmp_path, 'echo \'{"loggedIn": false}\'\n')
    monkeypatch.setattr(
        "oatutor_council.llm.claude_cli.auth_status", lambda s, **k: {"loggedIn": False}
    )
    with pytest.raises(ConfigurationError) as error:
        require_authentication(settings(tmp_path, executable))
    assert "claude auth login" in str(error.value)


def test_startup_refuses_an_api_key_login(tmp_path, monkeypatch):
    executable = fake_cli(tmp_path, "true\n")
    monkeypatch.setattr(
        "oatutor_council.llm.claude_cli.auth_status",
        lambda s, **k: {"loggedIn": True, "authMethod": "apiKey"},
    )
    with pytest.raises(ConfigurationError) as error:
        require_authentication(settings(tmp_path, executable))
    assert "subscription" in str(error.value).lower()


def test_startup_never_runs_the_login_flow(tmp_path, monkeypatch):
    """This application must never handle a password or a token."""
    executed: list[list[str]] = []

    def spy(command, **kwargs):
        executed.append(list(command))

        class Result:
            returncode = 0
            stdout = '{"loggedIn": true, "authMethod": "claude.ai"}'
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", spy)
    require_authentication(settings(tmp_path, fake_cli(tmp_path, "true\n")))

    assert executed, "auth status was never checked"
    for command in executed:
        assert "login" not in command
        assert command[1:3] == ["auth", "status"]
