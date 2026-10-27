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

let epoch = 0;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.now() + (epoch += 1_000_000)); // past any buzz an earlier test left running
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
    vi.advanceTimersByTime(500);
    haptic("logged");
    expect(vibrate.mock.calls).toEqual([[[8]], [[30, 130, 14, 106, 14]]]);
    expect(plugin.impact).not.toHaveBeenCalled();
  });

  it("plays moments one after another, because a new vibrate call would cut the running one short", () => {
    const calls: { at: number; pattern: number[] }[] = [];
    vi.stubGlobal("navigator", { vibrate: (pattern: number[]) => calls.push({ at: Date.now(), pattern }) });
    haptic("logged");   // 294 ms
    haptic("tick");
    haptic("tick");
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(2000);
    expect(calls.map((c) => c.pattern)).toEqual([[30, 130, 14, 106, 14], [8], [8]]);
    // Each starts only after the one before has finished.
    expect(calls[1].at - calls[0].at).toBeGreaterThanOrEqual(294);
    expect(calls[2].at - calls[1].at).toBeGreaterThanOrEqual(8);
  });

  it("drops a tap that would have to wait too long to mean anything", () => {
    const vibrate = vi.fn();
    vi.stubGlobal("navigator", { vibrate });
    for (let i = 0; i < 6; i++) haptic("logged");
    vi.advanceTimersByTime(10000);
    expect(vibrate.mock.calls.length).toBeLessThan(6);
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
