import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const plugin = vi.hoisted(() => ({ impact: vi.fn(), notification: vi.fn(), selectionStart: vi.fn(), selectionChanged: vi.fn(), selectionEnd: vi.fn() }));
const platform = vi.hoisted(() => ({ native: true, available: true }));
vi.mock("@capacitor/haptics", () => ({
  Haptics: plugin,
  ImpactStyle: { Light: "LIGHT", Medium: "MEDIUM", Heavy: "HEAVY" },
  NotificationType: { Success: "SUCCESS" },
}));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => platform.native, isPluginAvailable: () => platform.available },
}));

import { haptic, hapticChannel, LEVELS, onHapticOutcome, playScore, webPulse } from "./haptics";

let epoch = 0;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.now() + (epoch += 1_000_000)); // past any buzz an earlier test left running
  platform.native = true;
  platform.available = true;
  plugin.impact.mockResolvedValue(undefined);
  plugin.notification.mockResolvedValue(undefined);
  for (const fn of [plugin.selectionStart, plugin.selectionChanged, plugin.selectionEnd]) fn.mockResolvedValue(undefined);
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
    vi.advanceTimersByTime(5000); // a note takes longer than that
    haptic("recordStop");
    expect(plugin.impact.mock.calls).toEqual([[{ style: "MEDIUM" }], [{ style: "LIGHT" }]]);
  });

  it("lets a moment finish before the next starts, because Android's vibrator would replace it", () => {
    haptic("logged"); // success (about 130 ms), then taps at 160 and 280 ms
    haptic("tick");
    expect(plugin.notification).toHaveBeenCalledTimes(1);
    expect(plugin.impact).not.toHaveBeenCalled();
    vi.advanceTimersByTime(279);
    expect(plugin.impact).toHaveBeenCalledTimes(1); // only logged's first tap, at 160
    vi.advanceTimersByTime(1000);
    expect(plugin.impact).toHaveBeenCalledTimes(3); // logged's second tap, then the queued tick
  });

  it("uses the native plugin, never the iPhone switch, and says when it fails", async () => {
    vi.stubGlobal("navigator", { userAgent: "iPhone OS 18_0", maxTouchPoints: 5, userActivation: { isActive: true } });
    expect(hapticChannel()).toBe("native");
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    plugin.impact.mockRejectedValue(new Error("UNIMPLEMENTED"));
    haptic("tick");
    await vi.advanceTimersByTimeAsync(10);
    stop();
    expect(heard).toEqual(["tick native: failed (UNIMPLEMENTED)"]);
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

  it("drops a tick that would have to wait too long to mean anything, and says so", () => {
    const vibrate = vi.fn();
    vi.stubGlobal("navigator", { vibrate });
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    for (let i = 0; i < 3; i++) haptic("logged");
    haptic("tick");
    stop();
    vi.advanceTimersByTime(10000);
    expect(vibrate).toHaveBeenCalledTimes(3);
    expect(heard).toContain("tick: dropped (another buzz was still running)");
  });

  it("never drops a moment that answers the person's own action, however busy", () => {
    const vibrate = vi.fn();
    vi.stubGlobal("navigator", { vibrate });
    for (let i = 0; i < 6; i++) haptic("logged");
    haptic("recordStop");
    vi.advanceTimersByTime(20000);
    expect(vibrate).toHaveBeenCalledTimes(7);
  });

  it("reports a refused vibrate call", () => {
    vi.stubGlobal("navigator", { vibrate: () => false });
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    haptic("tick");
    return Promise.resolve().then(() => {
      stop();
      expect(heard).toEqual(["tick vibrate: failed (navigator.vibrate returned false)"]);
    });
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

describe("a score (the suggestion animation's pulses, #144)", () => {
  it("plays each level with the native preset that matches it, softest first, at its time", async () => {
    playScore(LEVELS.map((level, i) => ({ at: i * 200, level })));
    await vi.advanceTimersByTimeAsync(0);
    expect(plugin.selectionChanged).toHaveBeenCalledTimes(1); // soft: a selection tick, the gentlest there is
    expect(plugin.selectionStart.mock.invocationCallOrder[0]).toBeLessThan(plugin.selectionChanged.mock.invocationCallOrder[0]);
    expect(plugin.impact).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2000);
    expect(plugin.impact.mock.calls).toEqual([[{ style: "LIGHT" }], [{ style: "MEDIUM" }], [{ style: "MEDIUM" }], [{ style: "HEAVY" }]]);
    expect(plugin.notification).toHaveBeenCalledWith({ type: "SUCCESS" });
  });

  it("on Android browsers, stands in for strength with how long each buzz is", () => {
    const vibrate = vi.fn((_pattern: number[]) => true);
    vi.stubGlobal("navigator", { vibrate });
    platform.native = false;
    playScore(LEVELS.map((level, i) => ({ at: i * 200, level })));
    vi.advanceTimersByTime(2000);
    expect(vibrate.mock.calls.map(([pattern]) => pattern)).toEqual(LEVELS.map(webPulse));
    const lengths = LEVELS.map((level) => webPulse(level).reduce((sum, ms) => sum + ms, 0));
    expect(lengths).toEqual([...lengths].sort((a, b) => a - b)); // softer is shorter
  });

  it("does not hold its own pulses back, even when they come close together", () => {
    playScore([{ at: 0, level: "heavy" }, { at: 20, level: "heavy" }]);
    vi.advanceTimersByTime(100);
    expect(plugin.impact).toHaveBeenCalledTimes(2);
  });

  it("gives way to a moment that is buzzing, and a moment waits for a pulse that is", () => {
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    haptic("logged"); // the person's own Save
    playScore([{ at: 50, level: "light" }]);
    vi.advanceTimersByTime(60);
    expect(heard).toContain("score light: dropped (another buzz was still running)");
    vi.advanceTimersByTime(2000);
    plugin.notification.mockClear();
    playScore([{ at: 0, level: "heavy" }]);
    haptic("filled");
    expect(plugin.notification).not.toHaveBeenCalled(); // not on top of the heavy tap
    vi.advanceTimersByTime(200);
    expect(plugin.notification).toHaveBeenCalledTimes(1);
    stop();
  });

  it("skips a pulse whose last check says no, and stops the rest when told", () => {
    let chip = true;
    const cancel = playScore([{ at: 100, level: "light", still: () => chip }, { at: 200, level: "heavy" }, { at: 300, level: "success" }]);
    chip = false;
    vi.advanceTimersByTime(250);
    expect(plugin.impact.mock.calls).toEqual([[{ style: "HEAVY" }]]);
    cancel();
    vi.advanceTimersByTime(1000);
    expect(plugin.notification).not.toHaveBeenCalled();
  });

  it("stays quiet, and says so, where nothing can buzz", () => {
    platform.native = false;
    vi.stubGlobal("navigator", {});
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    expect(() => playScore([{ at: 0, level: "heavy" }], "suggestion")).not.toThrow();
    stop();
    expect(heard).toEqual(["suggestion heavy: silent (no haptics channel here)"]);
  });
});
