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
//
// Session persistence: the active session ID is stored in sessionStorage so a
// page refresh restores the workspace rather than returning to the selector.
// A "Menu" button in the header clears the session and navigates back.

import { useCallback, useEffect, useState } from "react";

import { ApiError, getSession, startSession } from "./api/client";
import { AppHeader } from "./components/AppHeader";
import { ConversationInterface } from "./components/ConversationInterface";
import { EntryModeSelector } from "./components/EntryModeSelector";
import { ExportPanel } from "./components/ExportControls";
import type { EntryMode, SessionState, VisualizationPayload } from "./types";
import { INITIAL_PAYLOAD } from "./visualization/useVisualization";
import { VisualizationView } from "./visualization/VisualizationView";

const SESSION_STORAGE_KEY = "icpb_active_session_id";

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
  const [restoring, setRestoring] = useState(true);
  const [visualization, setVisualization] = useState<VisualizationPayload>(INITIAL_PAYLOAD);
  const passphrase = null; // Wired to an access-guard prompt when protection is enabled.

  // On mount, try to restore a previously active session from sessionStorage.
  useEffect(() => {
    const savedId = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!savedId) {
      setRestoring(false);
      return;
    }
    getSession(savedId, passphrase)
      .then((state) => {
        setSession(state);
      })
      .catch(() => {
        // Session no longer exists on the backend — clear stale storage.
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
      })
      .finally(() => {
        setRestoring(false);
      });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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
      sessionStorage.setItem(SESSION_STORAGE_KEY, state.session_id);
      setSession(state);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not start the session.";
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const handleBack = useCallback(() => {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
    setSession(null);
    setVisualization(INITIAL_PAYLOAD);
    setError(null);
  }, []);

  // Show nothing while restoring a session on page load (avoids a flash of the
  // entry-mode selector before the session state arrives).
  if (restoring) {
    return (
      <div className="app app--start">
        <AppHeader />
      </div>
    );
  }

  if (session) {
    return (
      <div className="app app--workspace">
        <AppHeader onBack={handleBack} />
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
          onSelect={(mode) => start(mode)}
          busy={busy}
          error={error}
        />
      </main>
    </div>
  );
}
