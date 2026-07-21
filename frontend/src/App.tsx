// Top-level app shell wiring the whole UI together end-to-end (task 24.1).
//
// Before a session exists it presents the three entry modes (Req 24.1). Once a
// session is started it composes the three panels of the workspace around a
// single shared session socket:
//   - the ConversationInterface chat (task 23.1) owns the socket and lifts the
//     live visualization payload up via `onVisualization`,
//   - the Visualization_View (task 23.2) renders that per-turn-updated graph
//     (Req 5.3), and
//   - the ExportControls (task 23.3) drive the preview/diff/confirm and
//     build/export flow over the HTTP client.

import { useState } from "react";

import { ApiError, startSession } from "./api/client";
import { AppHeader } from "./components/AppHeader";
import { ConversationInterface } from "./components/ConversationInterface";
import { EntryModeSelector } from "./components/EntryModeSelector";
import { ExportPanel } from "./components/ExportControls";
import type { EntryMode, SessionState, VisualizationPayload } from "./types";
import { INITIAL_PAYLOAD } from "./visualization/useVisualization";
import { VisualizationView } from "./visualization/VisualizationView";

/** Generate a client session id, falling back when crypto.randomUUID is absent. */
function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
}

export function App() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visualization, setVisualization] = useState<VisualizationPayload>(INITIAL_PAYLOAD);
  const passphrase = null; // Wired to an access-guard prompt when protection is enabled.

  const start = async (
    mode: EntryMode,
    extras: { plugin_name?: string; source?: string; production_plugin?: string } = {},
  ) => {
    setBusy(true);
    setError(null);
    try {
      const state = await startSession(
        { entry_mode: mode, session_id: newSessionId(), ...extras },
        passphrase,
      );
      setSession(state);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not start the session.";
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  if (session) {
    return (
      <div className="app app--workspace">
        <AppHeader />
        <main className="workspace">
          <ConversationInterface
            session={session}
            passphrase={passphrase}
            onVisualization={setVisualization}
          />
          <div className="workspace__main">
            <div className="workspace__viz">
              <VisualizationView payload={visualization} />
            </div>
            <div className="workspace__export">
              <ExportPanel sessionId={session.session_id} passphrase={passphrase} />
            </div>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="app app--start">
      <AppHeader />
      <main>
        <EntryModeSelector
          // create_new starts an empty draft immediately (Req 24.2). iterate and
          // enhance require a target (a saved plugin / a production source) that a
          // dedicated browser supplies; started without one the backend returns a
          // clear error surfaced here (Req 24.3, 24.4).
          onSelect={(mode) => start(mode)}
          busy={busy}
          error={error}
        />
      </main>
    </div>
  );
}
