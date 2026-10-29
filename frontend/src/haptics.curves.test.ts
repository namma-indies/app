import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The app's own HapticCurves plugin (#145), on a phone that has curves.
const plugin = vi.hoisted(() => ({ impact: vi.fn(), notification: vi.fn(), selectionStart: vi.fn(), selectionChanged: vi.fn(), selectionEnd: vi.fn() }));
const curves = vi.hoisted(() => ({ capabilities: vi.fn(), play: vi.fn(), stop: vi.fn() }));
vi.mock("@capacitor/haptics", () => ({
  Haptics: plugin,
  ImpactStyle: { Light: "LIGHT", Medium: "MEDIUM", Heavy: "HEAVY" },
  NotificationType: { Success: "SUCCESS" },
}));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => true, isPluginAvailable: () => true },
  registerPlugin: (name: string) => (name === "HapticCurves" ? curves : null),
}));

import { haptic, hapticCurves, onHapticOutcome, playScore, type Pulse, type Ramp } from "./haptics";

const flight: Ramp = { at: 600, duration: 700, from: "light", to: "heavy" };
const beat: Pulse[] = [
  { at: 188, level: "light" },
  { at: 915, level: "light", ladderOnly: true },
  { at: 1090, level: "medium", ladderOnly: true },
  { at: 1216, level: "firm", ladderOnly: true },
  { at: 1300, level: "success" },
];

let epoch = 0;
beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.now() + (epoch += 1_000_000));
  curves.capabilities.mockResolvedValue({ curves: true, engine: "corehaptics" });
  curves.play.mockResolvedValue({ id: "p1" });
  curves.stop.mockResolvedValue(undefined);
  for (const fn of Object.values(plugin)) fn.mockResolvedValue(undefined);
  await hapticCurves(); // asked once per app run
});
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("a score on a phone with curves", () => {
  it("is one native curve: taps for the light-up and landing, a smooth build for the flight instead of its ticks", async () => {
    playScore(beat, "suggestion", [flight]);
    await vi.advanceTimersByTimeAsync(187);
    expect(curves.play).not.toHaveBeenCalled(); // Android replaces a running buzz: start at the first tap, not before
    await vi.advanceTimersByTimeAsync(1);
    expect(curves.play).toHaveBeenCalledTimes(1);
    const { taps, ramps } = curves.play.mock.calls[0][0];
    expect(taps.map((tap: { at: number }) => tap.at)).toEqual([0, 1300 - 188, 1300 - 188 + 110]); // the success is a tap, then a stronger one
    expect(taps[2].intensity).toBeGreaterThan(taps[1].intensity);
    expect(ramps).toHaveLength(1);
    expect(ramps[0].at).toBe(600 - 188);
    expect(ramps[0].duration).toBe(700);
    expect(ramps[0].to.intensity).toBeGreaterThan(ramps[0].from.intensity);
    expect(ramps[0].to.sharpness).toBeGreaterThan(ramps[0].from.sharpness);
    await vi.advanceTimersByTimeAsync(3000);
    expect(plugin.impact).not.toHaveBeenCalled(); // no ladder on top
  });

  it("stops the rest when a pulse's last check says no (the chip was answered on the way)", async () => {
    let chip = true;
    playScore(beat.map((pulse) => ({ ...pulse, still: () => chip })), "suggestion", [flight]);
    await vi.advanceTimersByTimeAsync(700);
    chip = false;
    await vi.advanceTimersByTimeAsync(300);
    expect(curves.stop).toHaveBeenCalledWith({ id: "p1" });
  });

  it("stops when told", async () => {
    const stop = playScore(beat, "suggestion", [flight]);
    await vi.advanceTimersByTimeAsync(200);
    stop();
    await vi.advanceTimersByTimeAsync(0);
    expect(curves.stop).toHaveBeenCalledWith({ id: "p1" });
  });

  it("gives way to a moment that is buzzing: the score is played as the ladder, pulse by pulse", async () => {
    haptic("logged");
    playScore([{ at: 0, level: "heavy" }, { at: 900, level: "light" }], "suggestion");
    await vi.advanceTimersByTimeAsync(1000);
    expect(curves.play).not.toHaveBeenCalled();
    expect(plugin.impact).toHaveBeenCalledWith({ style: "LIGHT" }); // the later pulse, once the moment was done
  });

  it("plays the ladder when asked to, to compare", async () => {
    playScore(beat, "suggestion", [flight], { ladder: true });
    await vi.advanceTimersByTimeAsync(2000);
    expect(curves.play).not.toHaveBeenCalled();
    expect(plugin.impact).toHaveBeenCalledTimes(4); // light, light, medium, firm
    expect(plugin.notification).toHaveBeenCalledTimes(1);
  });

  it("plays the very first score once the phone has said it has curves", async () => {
    vi.resetModules();
    const fresh = await import("./haptics");
    fresh.playScore([{ at: 0, level: "heavy" }], "suggestion");
    expect(curves.play).not.toHaveBeenCalled(); // still asking
    await vi.advanceTimersByTimeAsync(5);
    expect(curves.play).toHaveBeenCalledTimes(1);
    expect(plugin.impact).not.toHaveBeenCalled();
  });

  it("says when the native curve fails", async () => {
    curves.play.mockRejectedValue(new Error("engine stopped"));
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    playScore([{ at: 0, level: "heavy" }], "suggestion");
    await vi.advanceTimersByTimeAsync(10);
    stop();
    expect(heard).toEqual(["suggestion curves (corehaptics): failed (engine stopped)"]);
  });
});
