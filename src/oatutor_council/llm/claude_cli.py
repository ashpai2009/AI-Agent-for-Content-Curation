"""The production provider: the Claude Code CLI, on the user's subscription.

**Read `docs/claude-cli-notes.md` before changing anything here.** The flags below were
checked against the installed binary (`claude --version` → 2.1.219), not recalled.

There is no API key anywhere in this application. The CLI authenticates against the user's
Claude subscription through its own keychain-backed login, which has two consequences that
shape this whole module:

* **`--bare` must never be used.** It reads `ANTHROPIC_API_KEY` and explicitly never reads
  OAuth or the keychain — exactly backwards for this deployment. It would silently convert
  a subscription into usage-based API billing.
* **The child environment is an allowlist, not the parent's.** An inherited
  `ANTHROPIC_API_KEY` from a developer's shell would do the same thing just as quietly, so
  the variable is dropped on the way in rather than trusted not to be set.

Every call is a **fresh process in a fresh empty directory**, which is what makes context
isolation structural here rather than disciplinary: there is no session to resume, no
working directory whose files could be read, and no state that outlives the call. The
prompt goes in on **stdin** — never as an argument, never through the environment, never
via a temporary file — because arguments are visible in `ps` to every user on the machine
and a workbook is a curator's material.

Tools are disabled with an explicit `--tools ""`. The empty string is a real argument and
omitting it enables the full built-in set; an agent that could read files and run shell
commands is not an agent that merely reads the block it was given.

**One `complete()` starts exactly one process.** Retrying is deliberately somebody else's
job -- `RetryingClient` wraps `RecordingClient` wraps this -- so that every physical
`claude` invocation is charged to the job's budget and written to the audit trail on its
own. Retrying in here made a run that spent four calls on an outage look, in the record,
exactly like a run that spent one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Sequence

from ..config import Settings
from .base import (
    LLMRequest,
    LLMResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderOutputTooLarge,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUsageLimited,
    RateLimited,
    sanitize_provider_message,
)

#: Environment variables the child is allowed to see. Everything else is dropped.
#:
#: `HOME` is what lets the CLI find its own credentials, so it is not optional. The
#: Anthropic key variables are **deliberately absent**: their presence would flip the CLI
#: from the subscription to metered API billing without saying so, and a developer with one
#: exported in their shell should not be able to cause that by starting the service.
_ENVIRONMENT_ALLOWLIST = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    # macOS keychain access runs through the per-session bootstrap namespace; without it
    # the CLI cannot read the credentials it stored at login.
    "XPC_SERVICE_NAME",
    "__CF_USER_TEXT_ENCODING",
)

#: Variables that must never reach the child even if something adds them to the allowlist.
#: Belt and braces, and cheap: the failure they prevent is silent and expensive.
FORBIDDEN_CHILD_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

#: Cap on what is read back from the child. A provider that returns a gigabyte is a
#: provider that fills memory; the cap turns that into an ordinary failure.
#:
#: **Enforced while reading, not after.** The first version sliced the output once the
#: child had finished, which caps what is *kept* and bounds nothing at all: a runaway CLI
#: had already been buffered whole before the slice ran. The reader now stops at the cap,
#: kills the process group, and raises -- so the ceiling on memory is the cap plus one
#: chunk, whatever the child intended to send.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: Read size. Large enough that a normal response is a handful of reads, small enough that
#: overshooting the cap costs at most this much.
_CHUNK_BYTES = 64 * 1024


def child_environment(parent: dict[str, str] | None = None) -> dict[str, str]:
    """The restricted environment the CLI runs under.

    Built by allowlist rather than by deleting known-bad names: a denylist is a promise to
    have thought of every variable that matters, and the cost of being wrong here is
    billing a subscription user's card.
    """
    source = os.environ if parent is None else parent
    environment = {
        name: source[name]
        for name in _ENVIRONMENT_ALLOWLIST
        if name in source and source[name] is not None
    }
    for name in FORBIDDEN_CHILD_VARIABLES:
        environment.pop(name, None)
    return environment


def build_command(settings: Settings, request: LLMRequest) -> list[str]:
    """The exact argument list. No shell, so nothing here is ever word-split or expanded.

    Every flag is verified against the installed CLI. `--help` is **not** sufficient
    evidence for absence and this module learned that the expensive way: `--max-turns` was
    dropped here because the abbreviated help does not list it, when the binary carries
    both the flag and its description (`Maximum number of agentic turns in non-interactive
    mode`, print mode only) and the published CLI reference documents it. A flag that does
    not exist aborts every call, so it is checked against the binary before it is used --
    but so is the claim that a flag is missing.
    """
    effort = settings.effort_for(request.role.value)
    return [
        settings.claude_cli_path,
        "--print",
        "--output-format", "json",
        "--json-schema", json.dumps(request.schema),
        "--model", settings.claude_model,
        "--effort", effort,
        # Replaces the CLI's own system prompt rather than appending to it. The agent's
        # prompt is the whole instruction set; leaving Claude Code's coding-assistant
        # preamble underneath it would be a second, invisible set of instructions.
        "--system-prompt", request.system_prompt,
        # The empty string is load-bearing. Omit it and every built-in tool is enabled.
        "--tools", "",
        # Stated rather than inferred. `--tools ""` already leaves nothing to iterate on,
        # but that is an argument about what the model *should* have no reason to do; this
        # is the CLI refusing to let it. Two independent limits, because the thing being
        # bounded is spend on somebody's subscription.
        #
        # **The value is 2, and 1 was measured to be worse on both counts.** At 1 the live
        # pilot lost four independent-review calls to `Reached maximum number of turns (1)`
        # before the structured output was emitted; retry recovered every one, so the
        # ceiling meant to save calls was spending whole extra ones. A limit that fires on
        # correct work is not a limit, it is a retry loop with a confusing error message.
        "--max-turns", str(settings.claude_max_turns),
        "--safe-mode",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
    ]


class ClaudeCLIClient:
    """An `LLMClient` backed by one `claude --print` process per call.

    **Exactly one process per `complete()`, and that is a contract rather than an
    implementation detail.** Retrying lives outside, in `RetryingClient`, above the
    recorder -- because a retry that happens in here is a `claude` invocation that the
    audit trail never sees and the model-call budget never counts.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._settings = settings
        # Injected only so tests can drive the parsing and classification without a real
        # process. Production always uses `_run_process`.
        self._runner = runner or self._run_process

    # -- the LLMClient surface ---------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        command = build_command(self._settings, request)
        started = time.monotonic()

        # A fresh empty directory per call, outside the repository and outside every job
        # directory. The CLI treats its working directory as context, so running it inside
        # the project would put this source tree -- and a job directory would put a
        # curator's workbook -- within reach of a process that should see only stdin.
        with tempfile.TemporaryDirectory(prefix="council-cli-") as scratch:
            try:
                completed = self._runner(
                    command,
                    input=request.user_payload,
                    cwd=scratch,
                    timeout=self._settings.provider_timeout_seconds,
                )
            except FileNotFoundError as error:
                raise ProviderConfigurationError(
                    f"the claude executable was not found at "
                    f"{self._settings.claude_cli_path!r}. Install Claude Code or set "
                    f"COUNCIL_CLAUDE_CLI_PATH.",
                    status="executable_missing",
                ) from error
            except subprocess.TimeoutExpired as error:
                raise ProviderTimeout(
                    f"the claude CLI did not finish within "
                    f"{self._settings.provider_timeout_seconds:.0f}s"
                ) from error

        elapsed_ms = round((time.monotonic() - started) * 1000)
        return self._interpret(completed, elapsed_ms)

    # -- process --------------------------------------------------------------------

    @staticmethod
    def _run_process(
        command: Sequence[str], *, input: str, cwd: str, timeout: float
    ) -> subprocess.CompletedProcess:
        """Run the CLI, reading as it goes, and take the **whole process group** down.

        Two failures are guarded here and they need different machinery.

        *A timeout* is why the child gets its own session. `subprocess.run(timeout=...)`
        kills only the direct child; the CLI spawns its own helpers, and an orphan keeps a
        lease-holding worker's descriptors open and can keep talking to the provider after
        we have stopped listening. `start_new_session` means one `killpg` ends all of it.

        *A runaway* is why `communicate()` is gone. It buffers the entire response before
        anything can look at it, so a cap applied to its return value bounds what is kept
        and not what is held -- the memory was already spent. Reading in bounded chunks
        turns "the child sent a gigabyte" into a failure rather than an allocation.

        Three threads because pipes deadlock otherwise: a payload larger than the pipe
        buffer blocks on the write while the child blocks on a full stdout, and neither
        side ever moves.
        """
        process = subprocess.Popen(  # noqa: S603 - list form, shell=False, fixed argv
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=child_environment(),
            # Bytes, not text. The cap is a cap on memory, and only a byte count means
            # that; a character count under a multi-byte decoder does not.
            text=False,
            shell=False,
            start_new_session=True,
        )

        stdout, stderr = _Capture(), _Capture()
        overflowed = threading.Event()

        def on_overflow() -> None:
            """Stop the child the moment it goes past the cap, not once it finishes."""
            overflowed.set()
            _terminate_group(process)

        workers = [
            threading.Thread(
                target=_write_stdin, args=(process.stdin, input.encode()), daemon=True
            ),
            threading.Thread(
                target=_read_capped,
                args=(process.stdout, stdout, MAX_OUTPUT_BYTES, on_overflow),
                daemon=True,
            ),
            threading.Thread(
                target=_read_capped,
                args=(process.stderr, stderr, MAX_OUTPUT_BYTES, on_overflow),
                daemon=True,
            ),
        ]
        for worker in workers:
            worker.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_group(process)
            raise
        finally:
            for worker in workers:
                worker.join(timeout=5)

        if overflowed.is_set():
            raise ProviderOutputTooLarge(
                f"the claude CLI produced more than {MAX_OUTPUT_BYTES} bytes and was "
                "stopped; the process group was terminated"
            )

        return subprocess.CompletedProcess(
            args=list(command),
            returncode=process.returncode,
            stdout=stdout.text(),
            stderr=stderr.text(),
        )

    # -- interpreting the result ------------------------------------------------------

    def _interpret(
        self, completed: subprocess.CompletedProcess, elapsed_ms: int
    ) -> LLMResponse:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        if completed.returncode != 0:
            raise classify_cli_failure(completed.returncode, stdout, stderr)

        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "the claude CLI returned output that is not JSON; "
                f"{sanitize_provider_message(stdout[:200] or stderr[:200])}",
                status="malformed_envelope",
            ) from error

        if not isinstance(envelope, dict):
            raise ProviderError(
                "the claude CLI envelope was not a JSON object",
                status="malformed_envelope",
            )

        # An envelope can report failure with a zero exit status; the two disagree in the
        # cases that matter most (a usage limit, a refusal), so both are checked.
        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            raise classify_envelope_failure(envelope)

        text = extract_structured_output(envelope)
        return LLMResponse(
            text=text,
            model=str(envelope.get("model") or self._settings.claude_model),
            status="completed",
            usage=extract_usage(envelope, elapsed_ms),
        )


@dataclass
class _Capture:
    """What one pipe produced, bounded."""

    chunks: list[bytes] = field(default_factory=list)
    total: int = 0

    def text(self) -> str:
        # `replace` rather than `strict`: a truncated multi-byte sequence at a chunk
        # boundary is a diagnostic, and refusing to decode it would replace a usable
        # error message with a UnicodeDecodeError about the error message.
        return b"".join(self.chunks).decode("utf-8", "replace")


def _write_stdin(stream: IO[bytes] | None, payload: bytes) -> None:
    """Feed the payload and close, tolerating a child that is already gone."""
    if stream is None:  # pragma: no cover - stdin is always a pipe here
        return
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        # The child exited before reading its input -- an unknown flag, a missing
        # credential. Its exit status and stderr are the real diagnosis; a traceback from
        # this thread would only bury them.
        pass
    finally:
        try:
            stream.close()
        except (BrokenPipeError, OSError, ValueError):  # pragma: no cover
            pass


def _read_capped(
    stream: IO[bytes] | None, capture: _Capture, cap: int, on_overflow: Callable[[], None]
) -> None:
    """Accumulate up to `cap` bytes, then stop the child rather than keep reading."""
    if stream is None:  # pragma: no cover - both pipes are always present here
        return
    try:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                return
            capture.total += len(chunk)
            if capture.total > cap:
                on_overflow()
                return
            capture.chunks.append(chunk)
    except (OSError, ValueError):  # pragma: no cover - pipe closed under a kill
        return
    finally:
        try:
            stream.close()
        except (OSError, ValueError):  # pragma: no cover
            pass


def _terminate_group(process: subprocess.Popen) -> None:
    """SIGTERM the group, then SIGKILL what is left."""
    import signal

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


# --------------------------------------------------------------------------------------
# Envelope handling
# --------------------------------------------------------------------------------------

#: Where structured output can appear, in the order the CLI is expected to use. Checked
#: rather than assumed: an envelope shape that changes should fail loudly here, not turn
#: into a schema error that blames the model for a response it never gave.
_STRUCTURED_KEYS = ("structured_output", "structuredOutput", "structured_result")


def extract_structured_output(envelope: dict[str, Any]) -> str:
    """The agent's JSON, as text, ready for the caller's Pydantic model to validate.

    Two validations, deliberately. The CLI validates against the schema it was given, and
    `call_structured` validates the same text against the model it came from. Trusting the
    first alone would make a change in the CLI's validation a silent change in what this
    system accepts into a curator's workbook.
    """
    for key in _STRUCTURED_KEYS:
        value = envelope.get(key)
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        if isinstance(value, str) and value.strip():
            return value

    result = envelope.get("result")
    if isinstance(result, (dict, list)):
        return json.dumps(result)
    if isinstance(result, str) and result.strip():
        return result

    raise ProviderError(
        "the claude CLI returned no structured output; the response carried "
        f"{sorted(envelope)[:8]}",
        status="no_structured_output",
    )


def extract_usage(envelope: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    """Everything the CLI reports about what the call consumed.

    Cache reads and cache writes are carried through because they are the difference
    between a saving that came from batching and one that came from the provider's own
    caching -- and a number that is not recorded cannot answer which.

    **No dollar figure is derived.** The CLI reports a cost estimate for API-key users;
    on a subscription it is meaningless, and a fabricated cost in a curator's report is
    worse than no cost at all.
    """
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def number(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    return {
        "input_tokens": number("input_tokens", "inputTokens"),
        "output_tokens": number("output_tokens", "outputTokens"),
        "cache_creation_tokens": number(
            "cache_creation_input_tokens", "cacheCreationInputTokens"
        ),
        "cache_read_tokens": number("cache_read_input_tokens", "cacheReadInputTokens"),
        "total_tokens": number("total_tokens", "totalTokens"),
        "duration_ms": envelope.get("duration_ms") or elapsed_ms,
        "model": envelope.get("model"),
        "result_status": envelope.get("subtype") or "success",
    }


# --------------------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------------------

_AUTH_SIGNALS = (
    "not logged in",
    "please run claude auth login",
    "authentication required",
    "invalid api key",
    "unauthorized",
    "401",
    "oauth token has expired",
    "credentials",
)

#: A subscription allowance, not a burst. Claude Pro has session and weekly windows, and
#: both behave like the plan quota the previous provider taught us to separate out.
_USAGE_LIMIT_SIGNALS = (
    "usage limit",
    "rate limit reached",
    "you've reached your",
    "out of usage",
    "weekly limit",
    "session limit",
    "upgrade to",
    "limit will reset",
)

_UNAVAILABLE_SIGNALS = (
    "overloaded",
    "server error",
    "internal server",
    "502",
    "503",
    "504",
    "connection reset",
    "network error",
    "temporarily",
)

_CONFIGURATION_SIGNALS = (
    "unknown model",
    "invalid model",
    "unrecognized option",
    "unknown option",
    "invalid json schema",
    "no such file or directory",
)

_REFUSAL_SIGNALS = (
    "refused",
    "cannot assist",
    "safety",
    "blocked by",
    "prohibited",
)


def _find(text: str, signals: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in signals)


def classify_cli_failure(returncode: int, stdout: str, stderr: str) -> ProviderError:
    """Map a non-zero exit onto something the council knows how to handle.

    Ordered so the specific beats the general: authentication and usage limits are checked
    before the transient classes, because both also look like ordinary failures and both
    need the opposite treatment from a retry.
    """
    combined = f"{stderr}\n{stdout}"
    message = sanitize_provider_message(
        (stderr.strip() or stdout.strip() or f"claude exited {returncode}")
    )

    if _find(combined, _AUTH_SIGNALS):
        return ProviderConfigurationError(
            "the claude CLI is not authenticated. Run `claude auth login` and restart "
            "the service.",
            status="unauthenticated",
        )
    if _find(combined, _USAGE_LIMIT_SIGNALS):
        return ProviderUsageLimited(message, resets_at=find_reset_time(combined))
    if _find(combined, _CONFIGURATION_SIGNALS):
        return ProviderConfigurationError(message, status="configuration")
    if _find(combined, _REFUSAL_SIGNALS):
        return ProviderRefused(message)
    if _find(combined, _UNAVAILABLE_SIGNALS):
        return ProviderUnavailable(message)

    # The conservative reading, unchanged from the previous provider: an unknown failure
    # treated as permanent fails jobs that would have succeeded, while the reverse costs a
    # bounded few retries.
    return ProviderError(message, status=f"exit_{returncode}")


def classify_envelope_failure(envelope: dict[str, Any]) -> ProviderError:
    """A zero exit status with a failure inside the envelope."""
    subtype = str(envelope.get("subtype") or "error")
    detail = envelope.get("result") or envelope.get("error") or subtype
    text = detail if isinstance(detail, str) else json.dumps(detail)
    message = sanitize_provider_message(text)

    if _find(text, _USAGE_LIMIT_SIGNALS) or subtype in ("usage_limit", "rate_limit"):
        return ProviderUsageLimited(message, resets_at=find_reset_time(text))
    if _find(text, _AUTH_SIGNALS):
        return ProviderConfigurationError(
            "the claude CLI is not authenticated. Run `claude auth login` and restart "
            "the service.",
            status="unauthenticated",
        )
    if _find(text, _REFUSAL_SIGNALS) or subtype == "refusal":
        return ProviderRefused(message)
    if subtype in ("error_max_turns", "error_during_execution"):
        return ProviderError(message, status=subtype)
    if _find(text, _UNAVAILABLE_SIGNALS):
        return ProviderUnavailable(message)
    return ProviderError(message, status=subtype)


def find_reset_time(text: str) -> str | None:
    """The provider's own reset time, when it states one. **Never inferred.**

    A window that says "resets at 3pm" can be waited out; a bare "weekly limit" cannot be
    turned into a timestamp without guessing, and a job that wakes on a guess spends a call
    to rediscover it is still limited. Absent means absent.
    """
    import re

    for pattern in (
        # `3pm` and `15:00` are both things a limit message says; requiring the minutes
        # would silently drop the commoner of the two and leave a job with no reset time
        # when one was stated.
        r"resets?\s+at\s+([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?[^.,\n]*)",
        r"resets?\s+(?:on|in)\s+([^.,\n]{3,40})",
        r"\"resets_at\"\s*:\s*\"([^\"]+)\"",
        r"try again (?:after|at)\s+([^.,\n]{3,40})",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


# --------------------------------------------------------------------------------------
# Startup checks
# --------------------------------------------------------------------------------------


def executable_present(settings: Settings) -> bool:
    path = settings.claude_cli_path
    return bool(shutil.which(path) or Path(path).is_file())


def auth_status(settings: Settings, *, timeout: float = 20.0) -> dict[str, Any]:
    """`claude auth status --json`, parsed. Never runs the login flow.

    A local credential check: it starts no session and sends nothing to a model, so it is
    safe at startup and safe in a readiness probe.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - list form, shell=False
            [settings.claude_cli_path, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_environment(),
            shell=False,
        )
    except FileNotFoundError:
        return {"loggedIn": False, "error": "executable_missing"}
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "error": "timeout"}

    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": "unparseable"}
    return parsed if isinstance(parsed, dict) else {"loggedIn": False, "error": "shape"}


def describe_authentication(status: dict[str, Any]) -> dict[str, Any]:
    """What is safe to serve over HTTP about the login.

    The account's email, organisation and identifiers are **not** included. A readiness
    probe answers "can this process do work", and everything past that is material for
    someone who should not have any.
    """
    method = str(status.get("authMethod") or "")
    return {
        "authenticated": bool(status.get("loggedIn")),
        # `claude.ai` is the subscription login. An API-key credential would report
        # something else, and this service is configured never to use one.
        "subscription_login": method == "claude.ai",
        "subscription_type": str(status.get("subscriptionType") or "") or None,
    }


def require_authentication(settings: Settings) -> None:
    """Refuse to accept work this process cannot do. Raises `ConfigurationError`.

    Checked at startup rather than at the first model call, for the same reason the
    credential check always was: a service that boots unauthenticated accepts a workbook,
    stores it, fails on its first call, and has cost the curator the upload and the wait to
    learn something that was knowable before they started.
    """
    from ..config import ConfigurationError

    if not executable_present(settings):
        raise ConfigurationError(
            f"the claude executable was not found at {settings.claude_cli_path!r}. "
            "Install Claude Code, or set COUNCIL_CLAUDE_CLI_PATH to its location."
        )

    described = describe_authentication(auth_status(settings))
    if not described["authenticated"]:
        raise ConfigurationError(
            "the Claude Code CLI is not logged in. Run `claude auth login` and restart "
            "the service. This application never handles your password or token."
        )
    if not described["subscription_login"]:
        raise ConfigurationError(
            "the Claude Code CLI is authenticated, but not with a Claude subscription "
            "login. This service is configured to run on a subscription and never on "
            "metered API billing. Run `claude auth login` to sign in with your account."
        )
