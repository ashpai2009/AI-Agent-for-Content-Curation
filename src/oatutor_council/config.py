"""Settings, read from the environment in exactly one place.

**There is no API key in this application.** The provider is the Claude Code CLI running on
the user's Claude subscription, which authenticates through its own keychain-backed login.
What lives here is where the executable is, which model to ask for, and how hard to think
-- never a credential.

A provider module that reaches for `os.environ` itself is a module that cannot be tested
without credentials and cannot be pointed at a different model without an edit.

Every budget is a setting rather than a constant because they are the system's fuses:
they exist to be tightened in an incident, and a fuse you have to redeploy to change is
not a fuse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: The three-attempt cap from the specification. Enforced in exactly one function --
#: `state_machine.IssueMachine.reserve_attempt` -- so there is one place to audit.
DEFAULT_MAX_REPAIR_ATTEMPTS = 3

#: Liveness and provider-failure defaults. Named constants rather than literals in two
#: places, so the dataclass default and the environment default cannot drift apart.
DEFAULT_HEARTBEAT_DIVISOR = 4
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
#: Real batched OpenStax audits exceeded 120 seconds while otherwise progressing. Five
#: minutes permits one slow structured response to finish and remains a hard bound on a
#: hung subprocess; killing at two minutes immediately paid for the same prompt again.
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 300.0
DEFAULT_PROVIDER_MAX_ATTEMPTS = 4
DEFAULT_PROVIDER_BACKOFF_CEILING_SECONDS = 60.0
DEFAULT_PROVIDER_FAILURE_BUDGET = 12
DEFAULT_RUN_DEADLINE_SECONDS = 21_600.0
DEFAULT_RETENTION_DAYS = 30.0

#: The model-call fuse has two layers. The absolute ceiling protects the subscription
#: from a malformed or unexpectedly large workbook; the size allowance prevents a tiny
#: pilot from inheriting that entire ceiling. These are deliberately generous relative
#: to the current offline topology measurements (14 logical calls for the five-block demo
#: and 45 for a 30-block shadow run), so the guardrail stops runaway repetition without
#: shortening any review stage.
DEFAULT_LLM_CALL_BUDGET = 300
DEFAULT_LLM_CALL_BASE_BUDGET = 20
DEFAULT_LLM_CALLS_PER_BLOCK_BUDGET = 6
#: Generated output (including provider-reported reasoning) gets its own fuse. Cache-read
#: tokens are intentionally excluded: they measure reused context traffic, not new model
#: generation, and treating them as spend would stop healthy cached jobs immediately.
DEFAULT_LLM_OUTPUT_TOKEN_BUDGET = 600_000
DEFAULT_LLM_OUTPUT_TOKEN_BASE_BUDGET = 40_000
DEFAULT_LLM_OUTPUT_TOKENS_PER_BLOCK_BUDGET = 10_000

#: Provider defaults. `sonnet` is an alias that tracks the latest model in that family.
#:
#: **The environment names are prefixed, and that is not decoration.** `CLAUDE_EFFORT` is a
#: variable the Claude Code CLI itself sets -- it was present and set to `high` in the shell
#: this migration was written in. An unprefixed setting would therefore be silently
#: overridden by whatever session happened to launch the service, from a source the operator
#: never configured and would not think to look at. The unprefixed names are **not** read as
#: fallbacks, because a fallback would reopen exactly that hole.
DEFAULT_CLAUDE_CLI_PATH = "claude"
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_CLAUDE_EFFORT = "medium"
VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
ROLE_EFFORT_ENV = {
    "initial_auditor": "COUNCIL_INITIAL_AUDITOR_EFFORT",
    "writer": "COUNCIL_WRITER_EFFORT",
    "known_issue_reviewer": "COUNCIL_KNOWN_ISSUE_REVIEWER_EFFORT",
    "independent_reviewer": "COUNCIL_INDEPENDENT_REVIEWER_EFFORT",
    "adjudicator": "COUNCIL_ADJUDICATOR_EFFORT",
    "final_verifier": "COUNCIL_FINAL_VERIFIER_EFFORT",
}

#: Turns per call. **4, measured rather than chosen.** Live pilots successively lost calls
#: to `Reached maximum number of turns` at ceilings 1, 2 and 3 before the model emitted its
#: structured output. Retrying each whole prompt was more expensive than allowing the
#: already-started invocation to reach turn 4.
#:
#: A setting because it is a fuse, and the reason to keep it low is unchanged -- what is
#: bounded is spend on somebody's subscription, and `--tools ""` already leaves nothing to
#: iterate on. Raise it further only with the same kind of evidence.
DEFAULT_CLAUDE_MAX_TURNS = 4

#: Scan batching. Two is the measured CLI-safe default: the first four-block live call on
#: realistic OpenStax material exceeded the 120-second process ceiling twice. Two still
#: halves repeated audit envelopes without betting a whole retry on an oversized prompt.
#: Count and rendered characters both bound it; one retains the legacy diagnostic path.
DEFAULT_SCAN_BATCH_SIZE = 2

#: Maximum issues repaired and reviewed together when they belong to one problem block.
#: Eight comfortably covers the real-workbook clusters measured so far while keeping the
#: response schema and simulated diff small. A value of one preserves the legacy path.
DEFAULT_REPAIR_BATCH_SIZE = 8

#: How many times one block may be scanned again because its coverage record was short of
#: its graded rows. **One**, not zero and not many: zero would make the coverage record a
#: report rather than a requirement, while an unbounded retry turns a model that keeps
#: omitting the same row into a job that never ends. When the budget is spent the rows are
#: recorded as never verified and the job carries on -- which denies it success and tells
#: a curator exactly which rows nothing looked at, rather than pretending either that the
#: rows are fine or that the workbook cannot be handed over.
DEFAULT_COVERAGE_RESCANS = 1

#: How many times one block may be sent to the Final Semantic Verifier. Every accepted
#: repair invalidates that block's verification, so without a bound a block whose repairs
#: keep uncovering more work would be verified forever. **Two**: one pass over the block as
#: the repair phases left it, and one more after a repair the verifier itself asked for.
#: When the bound is reached the block is left *unverified* rather than waved through --
#: the job then cannot report success, which is the honest answer, because the last thing
#: anyone established about that block predates its last edit.
DEFAULT_FINAL_SEMANTIC_ROUNDS = 2
MAX_SCAN_BATCH_SIZE = 16
#: A five-row block and a hundred-row block must not consume the same allowance, so the
#: batch is bounded by rendered size as well as by count.
DEFAULT_SCAN_BATCH_MAX_CHARACTERS = 24_000


class ConfigurationError(RuntimeError):
    """The service is not configured well enough to do the work it accepts.

    Raised at startup rather than at the first model call. A service that starts happily
    without credentials accepts uploads it can never process, and the curator finds out
    after the wait rather than before the upload.
    """


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, got {raw!r}") from error


@dataclass(frozen=True)
class Settings:
    #: Path to the `claude` executable. A name is resolved on `PATH`; an absolute path is
    #: used as given, which is what a deployment with a pinned install wants.
    claude_cli_path: str
    #: An alias (`sonnet`, `opus`) or a full model name. An alias tracks the latest model
    #: in that family, which is the right default for a service that is not pinned to a
    #: specific snapshot.
    claude_model: str
    #: Default reasoning effort. Overridable per role -- see `effort_for`.
    claude_effort: str
    data_root: Path

    max_repair_attempts: int
    max_validation_rounds: int
    step_budget: int
    llm_call_budget: int
    interrupted_retry_budget: int

    max_concurrent_jobs: int
    max_upload_bytes: int
    lease_seconds: int

    #: Agentic turns the CLI will allow per call. See `DEFAULT_CLAUDE_MAX_TURNS`.
    claude_max_turns: int = DEFAULT_CLAUDE_MAX_TURNS
    #: Size-aware part of the physical-call fuse. The effective allowance is
    #: `min(llm_call_budget, llm_call_base_budget + blocks *
    #: llm_calls_per_block_budget)` and is pinned when a job starts.
    llm_call_base_budget: int = DEFAULT_LLM_CALL_BASE_BUDGET
    llm_calls_per_block_budget: int = DEFAULT_LLM_CALLS_PER_BLOCK_BUDGET
    llm_output_token_budget: int = DEFAULT_LLM_OUTPUT_TOKEN_BUDGET
    llm_output_token_base_budget: int = DEFAULT_LLM_OUTPUT_TOKEN_BASE_BUDGET
    llm_output_tokens_per_block_budget: int = (
        DEFAULT_LLM_OUTPUT_TOKENS_PER_BLOCK_BUDGET
    )

    #: How often a running worker renews its lease. Must be comfortably shorter than
    #: `lease_seconds` or a worker loses a job it is actively working on -- see
    #: `heartbeat_seconds` below, which enforces exactly that rather than trusting it.
    heartbeat_divisor: int = DEFAULT_HEARTBEAT_DIVISOR
    #: How often the poller looks for jobs whose worker died.
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS

    #: Per model call. A call with no ceiling can hold a lease for the life of the process.
    provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    #: Total attempts per call, including the first. Bounded because every class of
    #: transient failure this system retries is one that repeats when it is not transient.
    provider_max_attempts: int = DEFAULT_PROVIDER_MAX_ATTEMPTS
    provider_backoff_ceiling_seconds: float = DEFAULT_PROVIDER_BACKOFF_CEILING_SECONDS
    #: How many model calls a job may lose to provider failure before the job is failed.
    #: Counted from durable events, so it survives the crash of the worker that spent them.
    provider_failure_budget: int = DEFAULT_PROVIDER_FAILURE_BUDGET
    #: Wall-clock ceiling for one worker's run of a job. Zero disables it.
    run_deadline_seconds: float = DEFAULT_RUN_DEADLINE_SECONDS

    #: Shared secret for every route except `/health`. Empty means the service is open,
    #: which is a legitimate configuration behind an authenticating proxy and a serious
    #: mistake anywhere else -- so it is reported by `/readyz` rather than assumed.
    api_token: str = ""
    #: How long a finished job's files and rows are kept. Zero keeps them forever, which
    #: is the wrong default for a service holding other people's course material.
    retention_days: float = DEFAULT_RETENTION_DAYS

    #: Per-role reasoning effort overrides, empty by default. Behind a setting because
    #: changing it changes detection quality, not just cost.
    role_effort: dict[str, str] | None = None

    #: How many problem blocks one Initial Auditor or Independent Reviewer call examines.
    #: 1 delegates to the unchanged single-block path.
    scan_batch_size: int = DEFAULT_SCAN_BATCH_SIZE
    #: Maximum confirmed issues included in one Writer and one block-review call.
    repair_batch_size: int = DEFAULT_REPAIR_BATCH_SIZE
    #: Ceiling on a batch's total rendered contribution -- blocks, applicable claims,
    #: deterministic findings and labels together, not `render_block` alone.
    scan_batch_max_characters: int = DEFAULT_SCAN_BATCH_MAX_CHARACTERS
    #: Re-scans allowed for a block whose audit did not account for every graded row.
    coverage_rescans: int = DEFAULT_COVERAGE_RESCANS
    #: Final semantic verifications allowed per block.
    final_semantic_rounds: int = DEFAULT_FINAL_SEMANTIC_ROUNDS

    def __post_init__(self) -> None:
        if self.llm_call_budget < 1:
            raise ConfigurationError("LLM_CALL_BUDGET must be at least 1")
        if self.llm_call_base_budget < 0:
            raise ConfigurationError("LLM_CALL_BASE_BUDGET cannot be negative")
        if self.llm_calls_per_block_budget < 1:
            raise ConfigurationError("LLM_CALLS_PER_BLOCK_BUDGET must be at least 1")
        if self.llm_output_token_budget < 1:
            raise ConfigurationError("LLM_OUTPUT_TOKEN_BUDGET must be at least 1")
        if self.llm_output_token_base_budget < 0:
            raise ConfigurationError("LLM_OUTPUT_TOKEN_BASE_BUDGET cannot be negative")
        if self.llm_output_tokens_per_block_budget < 1:
            raise ConfigurationError(
                "LLM_OUTPUT_TOKENS_PER_BLOCK_BUDGET must be at least 1"
            )
        if not 1 <= self.scan_batch_size <= MAX_SCAN_BATCH_SIZE:
            raise ConfigurationError(
                f"SCAN_BATCH_SIZE must be between 1 and {MAX_SCAN_BATCH_SIZE}, "
                f"got {self.scan_batch_size}"
            )
        if not 1 <= self.repair_batch_size <= MAX_SCAN_BATCH_SIZE:
            raise ConfigurationError(
                f"REPAIR_BATCH_SIZE must be between 1 and {MAX_SCAN_BATCH_SIZE}, "
                f"got {self.repair_batch_size}"
            )
        if self.scan_batch_max_characters < 1:
            raise ConfigurationError(
                "SCAN_BATCH_MAX_CHARACTERS must be positive; it bounds one call's context"
            )
        if self.final_semantic_rounds < 1:
            raise ConfigurationError(
                "FINAL_SEMANTIC_ROUNDS must be at least 1; zero would mean the corrected "
                "workbook is never verified"
            )
        if self.coverage_rescans < 0:
            raise ConfigurationError(
                "COVERAGE_RESCANS cannot be negative; it is a retry budget"
            )
        if self.claude_effort not in VALID_EFFORT_LEVELS:
            raise ConfigurationError(
                f"COUNCIL_CLAUDE_EFFORT must be one of {', '.join(VALID_EFFORT_LEVELS)}, "
                f"got {self.claude_effort!r}"
            )
        unknown_roles = set(self.role_effort or {}) - set(ROLE_EFFORT_ENV)
        if unknown_roles:
            raise ConfigurationError(
                "role_effort contains unknown role(s): "
                + ", ".join(sorted(unknown_roles))
            )
        invalid_role_effort = {
            role: effort
            for role, effort in (self.role_effort or {}).items()
            if effort not in VALID_EFFORT_LEVELS
        }
        if invalid_role_effort:
            role, effort = next(iter(invalid_role_effort.items()))
            raise ConfigurationError(
                f"{ROLE_EFFORT_ENV[role]} must be one of "
                f"{', '.join(VALID_EFFORT_LEVELS)}, got {effort!r}"
            )
        if self.claude_max_turns < 1:
            raise ConfigurationError(
                "COUNCIL_CLAUDE_MAX_TURNS must be at least 1; a call that is allowed no "
                f"turns cannot produce a response, got {self.claude_max_turns}"
            )

    @property
    def requires_authentication(self) -> bool:
        return bool(self.api_token.strip())

    @property
    def heartbeat_seconds(self) -> float:
        """The renewal interval, derived so it *cannot* be longer than the lease.

        Kept as a derived property rather than its own setting on purpose. Two independent
        numbers where one must stay under the other is a configuration mistake waiting to
        be made in an incident, and the failure it produces is subtle: a worker that is
        alive and mid-model-call loses its job to the poller, which starts a second worker
        on the same job. One number, and the relationship holds by construction.
        """
        divisor = max(2, self.heartbeat_divisor)
        return max(1.0, self.lease_seconds / divisor)

    @property
    def provider_configured(self) -> bool:
        """Whether the *settings* name a provider. Says nothing about authentication.

        Deliberately separate: a well-configured service whose CLI is logged out is a
        different problem from one that was never configured, and the readiness endpoint
        reports them apart.
        """
        return bool(self.claude_cli_path.strip() and self.claude_model.strip())

    def effort_for(self, role: str) -> str:
        """Reasoning effort for one agent role.

        Per role because the six roles do different work -- but **defaulted the same for
        all of them**, because lowering it is a quality change and not a cost trim. The
        auditor and the reviewers judge mathematics; a cheaper setting that misses defects
        also *increases* calls by producing more repair rounds. Any per-role value should
        be set only after comparing runs on labelled material.
        """
        return (self.role_effort or {}).get(role, self.claude_effort)

    def require_credentials(self) -> None:
        """Fail loudly and early rather than at the first model call.

        Delegates to the CLI adapter, which checks that the executable exists, that the CLI
        is logged in, and that the login is a *subscription* rather than an API credential.
        A service that boots unauthenticated accepts a workbook it can never process.
        """
        if not self.claude_cli_path.strip():
            raise ConfigurationError(
                "COUNCIL_CLAUDE_CLI_PATH is empty. Unset it to use `claude` from PATH, or give "
                "the path to the executable."
            )
        if not self.claude_model.strip():
            raise ConfigurationError(
                "COUNCIL_CLAUDE_MODEL is empty. Unset it to use the default, or name a model."
            )

        from .llm.claude_cli import require_authentication

        require_authentication(self)

    def describe_provider(self) -> dict[str, object]:
        """What is configured, in a form that is safe to serve over HTTP.

        No credential detail and no filesystem path. The executable is reported as
        *present or not*, never as a location: an operator needs the former and an attacker
        probing for the layout should learn nothing from the latter.
        """
        return {
            "provider": "claude-code-cli",
            "model": self.claude_model,
            "effort": self.claude_effort,
        }


def load_settings(*, env_file: str | Path | None = ".env") -> Settings:
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    role_effort = {
        role: os.environ[variable].strip()
        for role, variable in ROLE_EFFORT_ENV.items()
        if os.environ.get(variable, "").strip()
    }

    return Settings(
        claude_cli_path=os.environ.get("COUNCIL_CLAUDE_CLI_PATH", DEFAULT_CLAUDE_CLI_PATH).strip()
        or DEFAULT_CLAUDE_CLI_PATH,
        claude_model=os.environ.get("COUNCIL_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL).strip()
        or DEFAULT_CLAUDE_MODEL,
        claude_effort=os.environ.get("COUNCIL_CLAUDE_EFFORT", DEFAULT_CLAUDE_EFFORT).strip()
        or DEFAULT_CLAUDE_EFFORT,
        claude_max_turns=_int("COUNCIL_CLAUDE_MAX_TURNS", DEFAULT_CLAUDE_MAX_TURNS),
        data_root=Path(os.environ.get("DATA_ROOT", "./jobs")).expanduser(),
        max_repair_attempts=_int("MAX_REPAIR_ATTEMPTS", DEFAULT_MAX_REPAIR_ATTEMPTS),
        max_validation_rounds=_int("MAX_VALIDATION_ROUNDS", 2),
        step_budget=_int("STEP_BUDGET", 2000),
        llm_call_budget=_int("LLM_CALL_BUDGET", DEFAULT_LLM_CALL_BUDGET),
        interrupted_retry_budget=_int("INTERRUPTED_RETRY_BUDGET", 2),
        llm_call_base_budget=_int(
            "LLM_CALL_BASE_BUDGET", DEFAULT_LLM_CALL_BASE_BUDGET
        ),
        llm_calls_per_block_budget=_int(
            "LLM_CALLS_PER_BLOCK_BUDGET", DEFAULT_LLM_CALLS_PER_BLOCK_BUDGET
        ),
        llm_output_token_budget=_int(
            "LLM_OUTPUT_TOKEN_BUDGET", DEFAULT_LLM_OUTPUT_TOKEN_BUDGET
        ),
        llm_output_token_base_budget=_int(
            "LLM_OUTPUT_TOKEN_BASE_BUDGET", DEFAULT_LLM_OUTPUT_TOKEN_BASE_BUDGET
        ),
        llm_output_tokens_per_block_budget=_int(
            "LLM_OUTPUT_TOKENS_PER_BLOCK_BUDGET",
            DEFAULT_LLM_OUTPUT_TOKENS_PER_BLOCK_BUDGET,
        ),
        # One at a time by default. The CLI backend runs a subprocess per call against a
        # single interactive subscription; two jobs in parallel double the rate at which a
        # session allowance is consumed and make a usage limit twice as likely mid-run.
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 1),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 52_428_800),
        lease_seconds=_int("LEASE_SECONDS", 120),
        heartbeat_divisor=_int("HEARTBEAT_DIVISOR", DEFAULT_HEARTBEAT_DIVISOR),
        poll_interval_seconds=_float(
            "POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
        ),
        provider_timeout_seconds=_float(
            "PROVIDER_TIMEOUT_SECONDS", DEFAULT_PROVIDER_TIMEOUT_SECONDS
        ),
        provider_max_attempts=_int(
            "PROVIDER_MAX_ATTEMPTS", DEFAULT_PROVIDER_MAX_ATTEMPTS
        ),
        provider_backoff_ceiling_seconds=_float(
            "PROVIDER_BACKOFF_CEILING_SECONDS", DEFAULT_PROVIDER_BACKOFF_CEILING_SECONDS
        ),
        provider_failure_budget=_int(
            "PROVIDER_FAILURE_BUDGET", DEFAULT_PROVIDER_FAILURE_BUDGET
        ),
        run_deadline_seconds=_float("RUN_DEADLINE_SECONDS", DEFAULT_RUN_DEADLINE_SECONDS),
        api_token=os.environ.get("API_TOKEN", "").strip(),
        retention_days=_float("RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
        role_effort=role_effort or None,
        scan_batch_size=_int("SCAN_BATCH_SIZE", DEFAULT_SCAN_BATCH_SIZE),
        repair_batch_size=_int("REPAIR_BATCH_SIZE", DEFAULT_REPAIR_BATCH_SIZE),
        coverage_rescans=_int("COVERAGE_RESCANS", DEFAULT_COVERAGE_RESCANS),
        final_semantic_rounds=_int(
            "FINAL_SEMANTIC_ROUNDS", DEFAULT_FINAL_SEMANTIC_ROUNDS
        ),
        scan_batch_max_characters=_int(
            "SCAN_BATCH_MAX_CHARACTERS", DEFAULT_SCAN_BATCH_MAX_CHARACTERS
        ),
    )
