// Fetch wrappers for the backend HTTP routes (icplugin_builder/api/app.py).
//
// This is the single HTTP client the UI uses. The conversation shell starts a
// session here (`startSession`); the preview/diff/confirm and build/export
// controls (task 23.3) drive the two-step export flow here (`prepareExport`
// computes the reviewable preview per Req 16.1-16.4, and `confirmExport` runs
// the build + export only after explicit confirmation per Req 16.5, 16.6).
//
// The live WebSocket channel used for conversation turns lives in `socket.ts`.

import type {
  ConfirmExportBody,
  EntryMode,
  ExportOutcome,
  ExportPlan,
  SessionState,
} from "../types";

/** Raised for any non-2xx response, carrying the backend's error detail. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Optional access passphrase forwarded on every protected request (Req 17.1). */
export interface ClientOptions {
  baseUrl?: string;
  passphrase?: string | null;
}

/** Body for starting a session by entry mode (matches StartSessionRequest). */
export interface StartSessionRequest {
  entry_mode: EntryMode;
  session_id: string;
  user_id?: string;
  plugin_name?: string;
  source?: string;
  production_plugin?: string;
}

function headers(passphrase?: string | null): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (passphrase) {
    h["X-Access-Passphrase"] = passphrase;
  }
  return h;
}

async function parseOrThrow<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body?.detail) {
        detail = body.detail;
      }
    } catch {
      // Non-JSON error body; keep the status text.
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

/**
 * Start a session by entry mode and return its initial state (Req 24).
 * POST /api/session
 */
export async function startSession(
  request: StartSessionRequest,
  passphrase?: string | null,
  baseUrl = "",
): Promise<SessionState> {
  const response = await fetch(`${baseUrl}/api/session`, {
    method: "POST",
    headers: headers(passphrase),
    body: JSON.stringify(request),
  });
  return parseOrThrow<SessionState>(response);
}

/**
 * Compute the reviewable export preview without exporting (Req 12, 16).
 * POST /api/session/{sessionId}/export/prepare
 */
export async function prepareExport(
  sessionId: string,
  options?: ClientOptions,
): Promise<ExportPlan> {
  const base = options?.baseUrl ?? "";
  const response = await fetch(
    `${base}/api/session/${encodeURIComponent(sessionId)}/export/prepare`,
    { method: "POST", headers: headers(options?.passphrase) },
  );
  return parseOrThrow<ExportPlan>(response);
}

/**
 * Confirm/decline the preview and run the build + export (Req 16.5, 9, 10).
 * POST /api/session/{sessionId}/export/confirm
 */
export async function confirmExport(
  sessionId: string,
  body: ConfirmExportBody,
  options?: ClientOptions,
): Promise<ExportOutcome> {
  const base = options?.baseUrl ?? "";
  const response = await fetch(
    `${base}/api/session/${encodeURIComponent(sessionId)}/export/confirm`,
    {
      method: "POST",
      headers: headers(options?.passphrase),
      body: JSON.stringify(body),
    },
  );
  return parseOrThrow<ExportOutcome>(response);
}
