// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const isNativePlatform = vi.fn();
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => isNativePlatform() },
}));

const addListener = vi.fn();
const remove = vi.fn();
vi.mock("@capacitor/app", () => ({
  App: { addListener: (...args: unknown[]) => addListener(...args) },
}));

beforeEach(() => {
  isNativePlatform.mockReset();
  addListener.mockReset();
  remove.mockReset();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({}));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("listenForAuthLinks", () => {
  it("does nothing on web -- the browser already handles the link itself", async () => {
    isNativePlatform.mockReturnValue(false);
    const { listenForAuthLinks } = await import("./deepLink");
    listenForAuthLinks(vi.fn());
    expect(addListener).not.toHaveBeenCalled();
  });

  it("consumes an /auth/* universal link and notifies the caller", async () => {
    isNativePlatform.mockReturnValue(true);
    addListener.mockImplementation((_event, handler) => {
      handler({ url: "https://app.nammaindies.org/auth/magic-link/consume?token=abc" });
      return Promise.resolve({ remove });
    });
    const { listenForAuthLinks } = await import("./deepLink");
    const onConsumed = vi.fn();
    listenForAuthLinks(onConsumed);

    await new Promise((r) => setTimeout(r, 0));

    expect(fetch).toHaveBeenCalledWith(
      "https://app.nammaindies.org/auth/magic-link/consume?token=abc",
      { credentials: "include" },
    );
    expect(onConsumed).toHaveBeenCalled();
  });

  it("ignores links outside /auth/* -- a shared sighting link isn't an auth continuation", async () => {
    isNativePlatform.mockReturnValue(true);
    addListener.mockImplementation((_event, handler) => {
      handler({ url: "https://app.nammaindies.org/dex/some-sighting" });
      return Promise.resolve({ remove });
    });
    const { listenForAuthLinks } = await import("./deepLink");
    const onConsumed = vi.fn();
    listenForAuthLinks(onConsumed);

    await new Promise((r) => setTimeout(r, 0));

    expect(fetch).not.toHaveBeenCalled();
    expect(onConsumed).not.toHaveBeenCalled();
  });
});
