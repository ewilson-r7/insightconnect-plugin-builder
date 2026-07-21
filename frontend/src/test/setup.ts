// Vitest setup: extends `expect` with jest-dom matchers and cleans up the DOM
// between tests. Referenced by vite.config.ts `test.setupFiles`.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

// React Flow (used by the Visualization_View) measures DOM geometry via
// ResizeObserver / DOMMatrixReadOnly / matchMedia, none of which jsdom
// implements. Provide inert stubs so the graph renders under test.
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

if (!("DOMMatrixReadOnly" in globalThis)) {
  class DOMMatrixReadOnlyStub {
    m22 = 1;
  }
  (globalThis as unknown as Record<string, unknown>).DOMMatrixReadOnly = DOMMatrixReadOnlyStub;
}

// jsdom does not implement Element.scrollIntoView, which the MessageList calls
// to keep the newest chat message in view. Provide an inert stub.
if (
  typeof Element !== "undefined" &&
  typeof Element.prototype.scrollIntoView !== "function"
) {
  Element.prototype.scrollIntoView = function scrollIntoView(): void {};
}

if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
