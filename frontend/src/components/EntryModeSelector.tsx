// Entry-mode selection presented at session start (Req 24.1).
//
// Exactly three modes are offered: create a net-new plugin, iterate on a
// previously created custom plugin, and enhance an existing production plugin
// (Req 24.1). Selecting a mode starts a session via the backend; the parent
// supplies the async start callback so this component stays presentational.

import { useState } from "react";

import type { EntryMode } from "../types";
import { ENTRY_MODE_OPTIONS } from "./entryModes";

export interface EntryModeSelectorProps {
  /** Called with the chosen mode; may be async while the session is created. */
  onSelect: (mode: EntryMode) => void | Promise<void>;
  /** When set, disables the controls and shows the pending state. */
  busy?: boolean;
  /** An error surfaced from a failed session start. */
  error?: string | null;
}

export function EntryModeSelector({ onSelect, busy = false, error }: EntryModeSelectorProps) {
  const [pending, setPending] = useState<EntryMode | null>(null);

  const handleSelect = async (mode: EntryMode) => {
    if (busy || pending) {
      return;
    }
    setPending(mode);
    try {
      await onSelect(mode);
    } finally {
      setPending(null);
    }
  };

  return (
    <section className="entry-mode" aria-label="Choose how to start">
      <h1 className="entry-mode__title">How would you like to start?</h1>
      <ul className="entry-mode__options">
        {ENTRY_MODE_OPTIONS.map((option) => {
          const isPending = pending === option.mode;
          return (
            <li key={option.mode}>
              <button
                type="button"
                className="entry-mode__option"
                onClick={() => handleSelect(option.mode)}
                disabled={busy || pending !== null}
                aria-busy={isPending}
                data-mode={option.mode}
              >
                <span className="entry-mode__option-title">{option.title}</span>
                <span className="entry-mode__option-description">{option.description}</span>
                {isPending && <span className="entry-mode__option-pending">Starting…</span>}
              </button>
            </li>
          );
        })}
      </ul>
      {error && (
        <p className="entry-mode__error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
