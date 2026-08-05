// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const isNativePlatform = vi.fn();
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => isNativePlatform() },
}));

afterEach(() => {
  vi.resetModules();
});

describe("API_BASE", () => {
  it("is empty on web, so fetches stay same-origin", async () => {
    isNativePlatform.mockReturnValue(false);
    const { API_BASE } = await import("./apiBase");
    expect(API_BASE).toBe("");
  });

  it("points at the production host inside the native shell", async () => {
    isNativePlatform.mockReturnValue(true);
    const { API_BASE } = await import("./apiBase");
    expect(API_BASE).toBe("https://app.nammaindies.org");
  });
});
