# Claude Code CLI — the verified provider surface

Everything marked **verified** below was checked against the installed binary during the
migration. Everything marked **unverified** has not been observed and is handled
defensively in code; the smoke test is what closes that gap.

```
claude --version   →  2.1.219 (Claude Code)
which claude       →  a user-local install; the path is a setting, never hardcoded
```

---

## Authentication — verified

```
$ claude auth status --json
{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "apiProvider": "firstParty",
  "subscriptionType": "pro",
  ...
}
```

`--json` is the **default** for `auth status`; `--text` is the human form. The fields this
service reads are `loggedIn`, `authMethod` and `subscriptionType`. It never reads, logs or
serves `email`, `orgId` or `orgName` — a readiness probe answers "can this process work",
and the rest is material for somebody who should not have any.

**`authMethod == "claude.ai"` is the subscription login.** Anything else means the CLI
would bill through an API credential, which this service refuses to start on.

`claude auth login` is never invoked programmatically. This application never sees a
password or a token.

---

## The call — verified flags

Every flag below exists in 2.1.219's `--help`:

| Flag | Why |
| --- | --- |
| `--print` | Non-interactive; print and exit. |
| `--output-format json` | The envelope. `text` would leave nothing to validate. |
| `--json-schema <schema>` | Structured output validated against the agent's Pydantic schema. |
| `--model sonnet` | An alias tracking the latest model in that family. |
| `--effort <level>` | One of `low`, `medium`, `high`, `xhigh`, `max`. |
| `--system-prompt <prompt>` | **Replaces** the default. `--append-system-prompt` would leave Claude Code's own coding-assistant preamble underneath the agent's instructions — a second, invisible instruction set. |
| `--tools ""` | Disables every built-in tool. **The empty string is a real argument; omitting it enables all of them.** |
| `--safe-mode` | No CLAUDE.md, skills, plugins, hooks, MCP, custom agents or output styles. |
| `--disable-slash-commands` | No skills reachable through `/name`. |
| `--strict-mcp-config` + `--mcp-config '{"mcpServers":{}}'` | An explicitly empty MCP set, and every other MCP configuration ignored. |
| `--max-turns 1` | One turn, enforced by the CLI rather than inferred from having no tools. |
| `--permission-mode dontAsk` | Never blocks waiting for a human. With no tools there is nothing to permit, so this is belt and braces. |
| `--no-session-persistence` | Nothing written to disk, nothing resumable. |

### How "verified" is established — and a correction

`--max-turns` was **wrongly recorded here as not existing in 2.1.219**, on the evidence
that it is absent from the abbreviated `--help`. It is in the binary, with its own
description, and the published CLI reference documents it:

```
$ strings "$(readlink -f "$(which claude)")" | grep -A1 -- '--max-turns <turns>'
--max-turns <turns>
Maximum number of agentic turns in non-interactive mode. This will early exit the
conversation after the specified number of turns. (only works with --print)
```

The rule this produced: **`--help` is evidence a flag exists, never evidence it does
not.** The documentation explicitly says the help output is not exhaustive. A flag that
does not exist aborts every call, so presence must be checked — but so must absence,
against the binary and the reference, before "deliberately not used" is written down.

`--tools ""` still leaves nothing to iterate on, so the two limits are independent: one is
an argument about what the model has no reason to do, the other is the CLI refusing to let
it. Both, because what is being bounded is spend on somebody's subscription.

### Not used, deliberately

- **`--bare`** — reads `ANTHROPIC_API_KEY` and *never* reads OAuth or the keychain. It is
  precisely backwards here: it would silently convert a subscription into metered API
  billing.
- **`--dangerously-skip-permissions`**, `--continue`, `--resume`, `--fork-session`,
  `--chrome`, `--plugin-dir`, `--agents`, `--add-dir`, `--worktree`.

### Invocation properties

- **Argument list, `shell=False`.** Nothing is word-split or expanded.
- **The payload goes on stdin.** Never an argument (arguments are world-readable in `ps`),
  never an environment variable, never a temporary file. The payload is a curator's
  workbook.
- **A fresh empty temporary directory per call**, outside the repository and outside every
  job directory. The CLI treats its working directory as context.
- **A fresh process per call.** No session, no state, so context isolation between agents
  is structural rather than disciplinary.
- **A restricted child environment**, built by allowlist (`HOME`, `PATH`, locale, the macOS
  keychain-bootstrap variables). `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN` and friends are dropped on the way in — a developer with one
  exported in their shell must not be able to switch the service to API billing by
  starting it.
- **Timeout kills the process group.** `start_new_session=True` plus `killpg`, because the
  CLI spawns helpers and `subprocess.run(timeout=...)` only kills the direct child.
- **One `complete()` starts exactly one process.** Retrying lives outside, above the audit
  recorder, so every physical invocation is charged to the job's model-call budget and
  written to `llm_calls` on its own. While the retry was inside this client, four processes
  could produce one row and one charge: the trail understated the spend and the budget
  bounded a quarter of what it named.
- **Output is read incrementally and capped at 8 MB *while reading*.** The first version
  called `communicate()` and sliced its return value, which caps what is kept and bounds
  nothing — the whole response is buffered before the slice runs. Three threads (one
  writing stdin, one per output pipe) because a payload larger than the pipe buffer
  otherwise deadlocks against a child blocked on a full stdout. Exceeding the cap kills the
  process group and raises `ProviderOutputTooLarge`, which is **non-retryable**: the same
  prompt produces the same runaway.

### The adapter version

`CLI_ADAPTER_VERSION` in `council.py` is bumped whenever anything in this section changes
in a way that could alter results — a flag added or removed, the envelope read differently.
It is pinned per job and **compared on every resume**; a job pinned to a different adapter
than the running process fails as `CONFIG` rather than finishing under flags its first half
never saw. There is deliberately no output-limit formula pinned beside it: the CLI at
2.1.219 has no output-token ceiling to govern (`--max-thinking-tokens` and `--task-budget`
are different things), and metadata describing a mechanism that does not exist reads as
evidence that it does.

### A name collision worth knowing about

`CLAUDE_EFFORT` is **a variable Claude Code itself sets** — it was present and set to
`high` in the shell this migration was written in. Every setting for this service is
therefore prefixed `COUNCIL_`, and the unprefixed names are deliberately *not* read as
fallbacks. Otherwise a service launched from a Claude Code session inherits an effort level
from a source the operator never configured.

---

## The response envelope — **unverified**

The outer shape of `--output-format json` has **not** been observed; no live call has been
made. The parser therefore looks for structured output in each plausible location and
**fails loudly rather than guessing**, in this order:

1. `structured_output` / `structuredOutput` / `structured_result` — dict, list or string
2. `result` — dict, list or string
3. otherwise `ProviderError(status="no_structured_output")`, naming the keys that *were*
   present so the fix is one reading away

Failure is checked two ways, because they disagree in the cases that matter most — a usage
limit or a refusal can arrive with a zero exit status:

- a non-zero exit code → `classify_cli_failure`
- `is_error` true, or a `subtype` other than `success` → `classify_envelope_failure`

Usage is read from `usage` with both snake_case and camelCase spellings tried:
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`,
`total_tokens`. **No dollar figure is derived** — the CLI reports a cost estimate for
API-key users, and on a subscription that number is fiction.

**Running `scripts/smoke_claude_cli.py` is what confirms this section.** Update it with the
observed envelope afterwards, and delete the word "unverified".

---

## Subscription limits

A Claude Pro subscription has session and weekly allowances. They behave like the plan
quota the previous provider taught us to separate from a burst limit: **retrying does not
help and costs real time.** `ProviderUsageLimited` is non-retryable, fails the job
immediately rather than walking the failure budget down one call at a time, and leaves the
job **resumable** — nothing is misconfigured and the work is intact.

A reset time is preserved **only if the provider states one**. It is never inferred: a job
that wakes on a guessed timestamp spends a call to rediscover it is still limited.
