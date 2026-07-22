// Entry-mode selection presented at session start (Req 24.1).
//
// Exactly three modes are offered: create a net-new plugin, iterate on a
// previously created custom plugin, and enhance an existing production plugin.
// "Iterate Custom" prompts for the plugin name before starting.

import { useState } from "react";

import type { EntryMode } from "../types";
import { ENTRY_MODE_OPTIONS } from "./entryModes";

export interface EntryModeSelectorProps {
  /** Called with the chosen mode + extras; may be async while the session is created. */
  onSelect: (mode: EntryMode, extras?: { plugin_name?: string }) => void | Promise<void>;
  /** When set, disables the controls and shows the pending state. */
  busy?: boolean;
  /** An error surfaced from a failed session start. */
  error?: string | null;
}

export function EntryModeSelector({ onSelect, busy = false, error }: EntryModeSelectorProps) {
  const [pending, setPending] = useState<EntryMode | null>(null);
  const [promptingIterate, setPromptingIterate] = useState(false);
  const [pluginName, setPluginName] = useState("");

  const handleSelect = async (mode: EntryMode) => {
    if (busy || pending) {
      return;
    }
    // "Iterate Custom" needs a plugin name — show an input first.
    if (mode === "iterate_custom") {
      setPromptingIterate(true);
      return;
    }
    setPending(mode);
    try {
      await onSelect(mode);
    } finally {
      setPending(null);
    }
  };

  const handleIterateSubmit = async () => {
    if (!pluginName.trim()) return;
    setPending("iterate_custom");
    try {
      await onSelect("iterate_custom", { plugin_name: pluginName.trim() });
    } finally {
      setPending(null);
    }
  };

  if (promptingIterate) {
    return (
      <section className="entry-mode" aria-label="Iterate on a custom plugin">
        <h1 className="entry-mode__title">Which plugin do you want to iterate on?</h1>
        <p className="entry-mode__description">
          Enter the name of a plugin in your projects folder.
        </p>
        <div className="entry-mode__input-row">
          <input
            type="text"
            className="entry-mode__input"
            value={pluginName}
            onChange={(e) => setPluginName(e.target.value)}
            placeholder="e.g. rapid7_velociraptor"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter") handleIterateSubmit();
            }}
          />
          <button
            type="button"
            className="entry-mode__input-submit"
            onClick={handleIterateSubmit}
            disabled={!pluginName.trim() || busy || pending !== null}
          >
            {pending === "iterate_custom" ? "Starting…" : "Start"}
          </button>
        </div>
        <button
          type="button"
          className="entry-mode__back-link"
          onClick={() => setPromptingIterate(false)}
        >
          &larr; Back
        </button>
        {error && (
          <p className="entry-mode__error" role="alert">
            {error}
          </p>
        )}
      </section>
    );
  }

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
