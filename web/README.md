# Web interface

Upload a workbook, add anything specific you want the council to know, watch it run,
download the corrected file.

```bash
./scripts/serve.sh        # from the project root
```

That starts the council service on `:8000`, builds and serves the page on `:3000`, checks
that the two can talk to each other, and opens a browser. Ctrl-C stops both.

---

## Why this runs on your machine and not on a host

The council shells out to the Claude Code CLI, which authenticates against your Claude
subscription through this machine's keychain. There is no API key anywhere in this
application, deliberately — moving the work to a server would mean introducing one, and
turning a subscription into metered per-token billing.

Everything else follows from that. The service keeps SQLite in WAL mode on a writable
filesystem, holds job working copies on disk, and runs a single job for minutes under a
renewed lease. None of that fits a serverless function either. **The compute has to be
where the login is, and once it is, there is nothing left for a remote host to do.**

So this is a local application with a browser for a front end, which is a normal shape for
a tool that handles files you care about: nothing is uploaded anywhere, the workbook never
leaves the machine except as prompt text to Claude, and there is no service to keep running
when you are not using it.

### The page still has a server

`app/api/*` proxies every call to the council rather than letting the browser do it. On
localhost that is not really about secrecy; it is about keeping one shape:

- `COUNCIL_API_TOKEN` stays out of the JavaScript bundle. A `NEXT_PUBLIC_` variable is
  compiled into it, and "it is only localhost" is exactly how a token ends up somewhere
  it should not be later.
- No CORS to configure, now or if this is ever put behind something.

There is a test for the first one: the token appears in neither the page HTML nor any
served bundle.

---

## Configuration

`web/.env.local` — written for you on first run, and gitignored.

| Name | Value |
| --- | --- |
| `COUNCIL_API_URL` | `http://127.0.0.1:8000` |
| `COUNCIL_API_TOKEN` | the service's `API_TOKEN`, if it has one |

`API_TOKEN` is optional while everything is on localhost, and `serve.sh` says so when it is
unset rather than pretending otherwise. It matters the moment the service is reachable from
anywhere else.

If `.env.local` is ever lost or overwritten, it is two lines:

```bash
{ echo "COUNCIL_API_URL=http://127.0.0.1:8000"
  grep '^API_TOKEN=' ../.env | sed 's/^API_TOKEN=/COUNCIL_API_TOKEN=/'
} > .env.local
```

### Editing the page

`serve.sh` serves a production build and rebuilds when anything under `app/` or `lib/` is
newer than the last one. For live reload while changing the UI:

```bash
./scripts/serve.sh --api-only     # terminal 1
cd web && npm run dev             # terminal 2 — http://localhost:3000
```

---

## What the second box actually does

The standing curation rules — notation, dependencies, row types, LaTeX, multiple choice —
ship inside the service and are composed into all four agent prompts. They are never sent
to this page and never editable from it.

Text typed into "anything you want to add" is uploaded as an **instruction document**,
which is the mechanism the service already has. It is split into passages, each classified
`RULES`, `ERRATA` or `NOTES`, and routed accordingly:

- **errata** go to the Initial Auditor as claims, and only to the blocks a claim could be
  about. Each comes back confirmed, refuted or unresolved, and the report says which.
- **rules** go to the Initial Auditor, Writer and both reviewers as policy. Every passage
  accepted by the instruction reader is sent; there is no smaller routing cap.
- **notes** go only to the Initial Auditor as non-authoritative background. They are not
  a defect claim, do not authorize a change, and do not reach the Writer or reviewers.

The dropdown overrides the classification for the whole document. Either way the text is
rendered inside a fenced, labelled data section: it is content the agents read, not
instructions they follow, and it cannot override the system prompt.

The instruction reader's one bound is 200,000 extracted characters. The page prevents a
pasted note above that size; API-uploaded documents report truncation in the submission
response and final report rather than silently pretending the tail was read.

The result page separates **open issues** from **observations** using the backend's actual
repairability contract, not color or severity alone. A repairable warning such as trailing
whitespace remains open work; a non-repairable house-style warning is only an observation;
and a non-repairable error still needs a person.

---

## Limits worth knowing

- **A job makes real Claude calls on your subscription.** The progress card shows the
  running count. A thirty-block workbook needs roughly sixty calls before any repair.
- **One job at a time**, by design (`MAX_CONCURRENT_JOBS=1`): SQLite in WAL over one file
  wants one writer.
- **Closing the tab does not cancel a job.** The job is a durable row; the worker keeps
  going and the poller picks it up after a crash. The page stores the latest opaque job id
  in local browser storage, so reopening or refreshing resumes polling and download access.
- **The page remembers only the latest job.** It does not provide a multi-job history or
  cross-device account view; those require user identities and a retention policy rather
  than more browser storage.
- **50 MB upload guard** in `app/api/jobs/route.ts`, matching the council default. The
  backend remains authoritative if an operator configures a different limit.
