// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";

const platform = vi.hoisted(() => ({ native: true }));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => platform.native, isPluginAvailable: () => false, getPlatform: () => "ios" },
}));
vi.mock("@capacitor/app", () => ({ App: { getInfo: async () => ({ version: "1", build: "1" }), addListener: vi.fn() } }));
vi.mock("./screens/Capture", () => ({ default: () => null }));
vi.mock("./screens/Dex", () => ({ default: () => null }));
vi.mock("./screens/Admin", () => ({ default: () => null }));
vi.mock("./screens/SignIn", () => ({ default: () => null }));
vi.mock("./components/Celebration", () => ({ default: () => null }));
vi.mock("./offline/queue", () => ({
  failedCount: async () => 0, flush: async () => {}, setOnFlushed: () => {}, setOnUnauthorized: () => {},
}));
vi.mock("./api", () => ({
  getDex: async () => ({}), getMe: async () => ({ tier: "member" }), UnauthorizedError: class extends Error {},
}));

import App, { HAPTICS_HOLD_MS } from "./App";

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); platform.native = true; });
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function mount() {
  const view = render(<App />);
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  return view;
}

describe("opening the hidden Haptics check in the app", () => {
  it("opens after holding the brand for two seconds", async () => {
    const { container } = await mount();
    fireEvent.pointerDown(container.querySelector(".brand")!);
    await act(async () => { await vi.advanceTimersByTimeAsync(HAPTICS_HOLD_MS + 50); });
    expect(document.querySelector(".haptics-check")).not.toBeNull();
  });

  it("does not open for a shorter press", async () => {
    const { container } = await mount();
    const brand = container.querySelector(".brand")!;
    fireEvent.pointerDown(brand);
    await act(async () => { await vi.advanceTimersByTimeAsync(HAPTICS_HOLD_MS - 500); });
    fireEvent.pointerUp(brand);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(document.querySelector(".haptics-check")).toBeNull();
  });

  it("is not a gesture in a browser", async () => {
    platform.native = false;
    const { container } = await mount();
    fireEvent.pointerDown(container.querySelector(".brand")!);
    await act(async () => { await vi.advanceTimersByTimeAsync(HAPTICS_HOLD_MS + 50); });
    expect(document.querySelector(".haptics-check")).toBeNull();
  });
});
