import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const plugin = vi.hoisted(() => ({ impact: vi.fn(), notification: vi.fn() }));
const platform = vi.hoisted(() => ({ native: true, available: true }));
vi.mock("@capacitor/haptics", () => ({
  Haptics: plugin,
  ImpactStyle: { Light: "LIGHT", Medium: "MEDIUM" },
  NotificationType: { Success: "SUCCESS" },
}));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => platform.native, isPluginAvailable: () => platform.available },
}));

import { haptic } from "./haptics";

beforeEach(() => {
  vi.useFakeTimers();
  platform.native = true;
  platform.available = true;
  plugin.impact.mockResolvedValue(undefined);
  plugin.notification.mockResolvedValue(undefined);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("haptics on the native app", () => {
  it("ticks lightly", () => {
    haptic("tick");
    expect(plugin.impact).toHaveBeenCalledWith({ style: "LIGHT" });
  });

  it("answers a filled field with a success", () => {
    haptic("filled");
    expect(plugin.notification).toHaveBeenCalledWith({ type: "SUCCESS" });
  });

  it("makes the logged celebration a success followed by two taps, brief", () => {
    haptic("logged");
    expect(plugin.notification).toHaveBeenCalledTimes(1);
    expect(plugin.impact).not.toHaveBeenCalled();
    vi.advanceTimersByTime(160);
    expect(plugin.impact).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(120);
    expect(plugin.impact).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(5000);
    expect(plugin.impact).toHaveBeenCalledTimes(2);
  });

  it("marks the microphone going live and the note being captured differently", () => {
    haptic("recordStart");
    haptic("recordStop");
    expect(plugin.impact.mock.calls).toEqual([[{ style: "MEDIUM" }], [{ style: "LIGHT" }]]);
  });

  it("stays silent when the plugin rejects, throws, or is missing from an old build", async () => {
    plugin.impact.mockRejectedValue(new Error("UNIMPLEMENTED"));
    expect(() => haptic("tick")).not.toThrow();
    plugin.impact.mockImplementation(() => { throw new Error("boom"); });
    expect(() => haptic("tick")).not.toThrow();
    plugin.impact.mockClear();
    platform.available = false;
    expect(() => haptic("tick")).not.toThrow();
    await vi.advanceTimersByTimeAsync(10);
    expect(plugin.impact).not.toHaveBeenCalled();
  });
});

describe("haptics on the web", () => {
  beforeEach(() => { platform.native = false; });

  it("falls back to navigator.vibrate with the same shape", () => {
    const vibrate = vi.fn();
    vi.stubGlobal("navigator", { vibrate });
    haptic("tick");
    haptic("logged");
    expect(vibrate.mock.calls).toEqual([[[8]], [[30, 130, 14, 106, 14]]]);
    expect(plugin.impact).not.toHaveBeenCalled();
  });

  it("does nothing where there is no vibrate (iOS Safari, desktop)", () => {
    vi.stubGlobal("navigator", {});
    expect(() => haptic("filled")).not.toThrow();
  });

  it("waits for the first touch, which Chrome requires and complains about otherwise", () => {
    const vibrate = vi.fn();
    vi.stubGlobal("navigator", { vibrate, userActivation: { hasBeenActive: false } });
    haptic("tick");
    expect(vibrate).not.toHaveBeenCalled();
    vi.stubGlobal("navigator", { vibrate, userActivation: { hasBeenActive: true } });
    haptic("tick");
    expect(vibrate).toHaveBeenCalledTimes(1);
  });

  it("swallows a vibrate that throws", () => {
    vi.stubGlobal("navigator", { vibrate: () => { throw new Error("blocked"); } });
    expect(() => haptic("tick")).not.toThrow();
  });
});
