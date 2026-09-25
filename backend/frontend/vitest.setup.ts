import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { MockEventSource } from "./src/test/mock-event-source";

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.assign(globalThis, {
  EventSource: MockEventSource,
  ResizeObserver: NoopObserver,
});
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }),
});

afterEach(cleanup);
afterEach(() => {
  MockEventSource.instances = [];
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
  localStorage.clear();
});
