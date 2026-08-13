import { CouncilUnreachable, callCouncil, councilConfigured } from "@/lib/council";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Whether a job submitted right now could actually run.
 *
 * The council's own `/readyz` answers this without contacting a model — it describes the
 * provider and checks the local login, so polling it costs nothing. Only the fields the
 * page shows are forwarded; the account's email and organisation are not this page's
 * business and the service does not send them anyway.
 */
export async function GET(): Promise<Response> {
  if (!councilConfigured()) {
    return Response.json({
      ready: false,
      configured: false,
      detail:
        "COUNCIL_API_URL is not set. Set it to the address of the machine running the " +
        "council service.",
    });
  }

  try {
    const response = await callCouncil("/readyz");
    const body = (await response.json()) as Record<string, unknown>;
    return Response.json({
      ready: Boolean(body.ready),
      configured: true,
      checks: body.checks ?? {},
      authenticated: Boolean((body.checks as Record<string, unknown>)?.authenticated),
      subscription_type: body.subscription_type ?? null,
      model: body.model ?? null,
      detail: body.ready ? null : "the council service is not ready to run a job",
    });
  } catch (error) {
    return Response.json({
      ready: false,
      configured: true,
      detail:
        error instanceof CouncilUnreachable
          ? error.message
          : "the council service could not be reached",
    });
  }
}
