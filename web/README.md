# Web interface

Upload a workbook, add anything specific you want the council to know, watch it run,
download the corrected file.

---

## The one thing to understand first

**Vercel hosts this page. It cannot host the council.**

The council service shells out to the Claude Code CLI authenticated against your Claude
subscription, keeps SQLite in WAL mode on a writable filesystem, holds job working copies
on disk, and runs a single job for minutes under a renewed lease. A serverless function
has none of that: no `claude` binary, no keychain, an ephemeral filesystem, and a request
that ends long before an audit does.

So the split is:

```
browser  →  Vercel (this Next.js app)  →  your machine (FastAPI + Claude Code CLI)
                                             ↑ the subscription login lives here
```

Moving the council into the cloud would mean an Anthropic API key and metered billing —
exactly what the CLI migration removed. It stays where the login is.

**The browser never talks to the council directly.** Every call goes through a route
handler in `app/api/`, so `COUNCIL_API_TOKEN` stays on the server and there is no CORS to
configure. A `NEXT_PUBLIC_` variable would be compiled into the JavaScript bundle.

---

## Running it locally

Two terminals.

```bash
# 1 — the council
.venv/bin/uvicorn "oatutor_council.api:create_app" --factory --app-dir src --port 8000

# 2 — this app
cd web
npm install
cp .env.example .env.local     # the defaults already point at 127.0.0.1:8000
npm run dev                    # http://localhost:3000
```

The pill in the top right reads the council's `/readyz`. `ready` means a job submitted now
would actually run — the CLI is present, logged in on a subscription, the database is
reachable, and the worker pool and poller are up. It never contacts a model, so polling it
costs nothing.

---

## Deploying to Vercel

**1. Expose the council.** It listens on localhost; Vercel is not on your network.

```bash
cloudflared tunnel --url http://localhost:8000
# → https://something-random.trycloudflare.com
```

**2. Put a token on it.** A tunnel is a public URL. Without `API_TOKEN` the service is
open to anyone who finds it, and it accepts file uploads.

```bash
# in the project's .env
API_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
```

Restart the service. It logs a warning at startup when the token is unset, and `/readyz`
reports `authentication_required`.

**3. Deploy.**

```bash
cd web
npx vercel            # first run links the project
npx vercel --prod
```

**4. Set the environment variables** in the Vercel project settings (or
`npx vercel env add`):

| Name | Value |
| --- | --- |
| `COUNCIL_API_URL` | the tunnel URL, no trailing slash |
| `COUNCIL_API_TOKEN` | the same `API_TOKEN` the service has |

Redeploy after changing them — Next.js reads server env at request time, but Vercel only
applies new values to new deployments.

The tunnel URL changes every time you restart `cloudflared` unless you use a named tunnel.
A named tunnel with a stable hostname is worth setting up if this is more than a demo.

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
- **rules** go to the Writer and both reviewers as policy, capped at 4,000 characters
  because they ride on every call for the whole job.
- **notes** are recorded and not acted on.

The dropdown overrides the classification for the whole document. Either way the text is
rendered inside a fenced, labelled data section: it is content the agents read, not
instructions they follow, and it cannot override the system prompt.

---

## Limits worth knowing

- **4 MB upload cap**, enforced in `app/api/jobs/route.ts` because Vercel rejects request
  bodies over 4.5 MB. Real OATutor workbooks are 30–96 KB, so this is a guard rather than
  a ceiling anyone meets. A larger file has to go to the service directly.
- **The council runs one job at a time** by design (`MAX_CONCURRENT_JOBS=1`): SQLite in
  WAL over one file wants one writer. Two people submitting at once means the second waits.
- **Closing the tab does not cancel a job.** The job is a durable row; the worker keeps
  going and the poller picks it up after a crash. Re-open the page and it is gone from
  view, but the file is still produced — you would need the job id to fetch it.
- **This app holds no state.** Refreshing loses the job id. That is a deliberate floor
  rather than a design: adding a job list means storing curator job ids somewhere, and
  that is a decision about data retention, not a UI feature.
