// Entry-mode option metadata for the EntryModeSelector (Req 24.1).
//
// Kept in a non-component module so the selector component file exports only a
// component (satisfies the react-refresh lint rule) and so the option list can
// be reused by tests.

import type { EntryMode } from "../types";

export interface EntryModeOption {
  mode: EntryMode;
  title: string;
  description: string;
}

/** The three entry modes, in the order the design lists them (Req 24.1). */
export const ENTRY_MODE_OPTIONS: readonly EntryModeOption[] = [
  {
    mode: "create_new",
    title: "Create a net-new plugin",
    description: "Start from an empty draft and describe the plugin you want.",
  },
  {
    mode: "iterate_custom",
    title: "Iterate on a custom plugin",
    description: "Load a plugin you previously created and keep building on it.",
  },
  {
    mode: "enhance_production",
    title: "Enhance a production plugin",
    description: "Import a production plugin as a read-only fork and extend it.",
  },
];
