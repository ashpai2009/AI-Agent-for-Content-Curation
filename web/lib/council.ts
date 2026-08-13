/**
 * The only place this app talks to the council service.
 *
 * **Every call goes through the server**, never from the browser, and that is the whole
 * reason the route handlers in `app/api/` exist rather than the page calling the service
 * directly. Two consequences, both wanted:
 *
 * - `COUNCIL_API_TOKEN` stays server-side. A `NEXT_PUBLIC_` variable is compiled into the
 *   JavaScript bundle, which means published on the internet.
 * - No CORS. The council service is a private backend that never needs to name the
 *   origins allowed to call it, because only this server calls it.
 *
 * The council itself is **not** deployed here and cannot be. It shells out to the Claude
 * Code CLI authenticated against a Claude subscription, keeps SQLite in WAL mode on a
 * writable filesystem, and runs jobs for minutes under a lease. A serverless function has
 * none of those things. See `web/README.md`.
 */

export const COUNCIL_URL = (process.env.COUNCIL_API_URL || "").replace(/\/+$/, "");
const COUNCIL_TOKEN = process.env.COUNCIL_API_TOKEN || "";

/** How long to wait on the council service before calling it unreachable. */
const TIMEOUT_MS = 30_000;

export class CouncilUnreachable extends Error {}

export function councilConfigured(): boolean {
  return Boolean(COUNCIL_URL);
}

/**
 * Call the council service. Throws `CouncilUnreachable` when the service cannot be
 * reached at all, which is a different thing from the service answering with an error and
 * is worth telling the user apart: one means "check the tunnel", the other means "read
 * the message".
 */
export async function callCouncil(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  if (!COUNCIL_URL) {
    throw new CouncilUnreachable(
      "COUNCIL_API_URL is not set. Point it at the machine running the council service.",
    );
  }

  const headers = new Headers(init.headers);
  if (COUNCIL_TOKEN) {
    headers.set("authorization", `Bearer ${COUNCIL_TOKEN}`);
  }

  try {
    return await fetch(`${COUNCIL_URL}${path}`, {
      ...init,
      headers,
      // A job list that is one poll stale looks like a job that stopped making progress.
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    throw new CouncilUnreachable(
      `could not reach the council service at ${redactHost(COUNCIL_URL)}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

/**
 * JSON straight through, with the council's own status code preserved.
 *
 * The service's error messages are written for a curator and deliberately contain no
 * filesystem paths, so they are safe to forward verbatim -- and forwarding them beats
 * replacing a specific message ("instructions_purpose must be one of ...") with a generic
 * one.
 */
export async function proxyJson(path: string, init?: RequestInit): Promise<Response> {
  try {
    const response = await callCouncil(path, init);
    const body = await response.text();
    return new Response(body || "{}", {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  } catch (error) {
    if (error instanceof CouncilUnreachable) {
      return Response.json({ error: error.message, unreachable: true }, { status: 502 });
    }
    throw error;
  }
}

/** The host without credentials, for a message that may be shown to a user. */
function redactHost(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.protocol}//${parsed.host}`;
  } catch {
    return "the configured URL";
  }
}
