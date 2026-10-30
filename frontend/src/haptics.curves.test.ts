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

const RAMP_CHECK = 100; // how often a playing ramp asks again (haptics.ts)

const flight: Ramp = { at: 600, duration: 700, from: "light", to: "heavy" };
const beat: Pulse[] = [
  { at: 188, level: "light" },
  { at: 915, level: "light", ladderOnly: true },
  { at: 1090, level: "medium", ladderOnly: true },
  { at: 1216, level: "firm", ladderOnly: true },
  { at: 1300, level: "success" },
];

const at = { notifications: [] as number[] };
let epoch = 0;
beforeEach(async () => {
  at.notifications = [];
  vi.useFakeTimers();
  vi.setSystemTime(Date.now() + (epoch += 1_000_000));
  curves.capabilities.mockResolvedValue({ curves: true, engine: "corehaptics" });
  curves.play.mockResolvedValue({ id: "p1" });
  curves.stop.mockResolvedValue(undefined);
  for (const fn of Object.values(plugin)) fn.mockResolvedValue(undefined);
  plugin.notification.mockImplementation(async () => { at.notifications.push(Date.now()); });
  await hapticCurves(); // asked once per app run
});
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("a score on a phone with curves", () => {
  it("is native curves: a tap for the light-up, then one smooth build for the flight, ending on the landing, instead of its ticks", async () => {
    playScore(beat, "suggestion", [flight]);
    await vi.advanceTimersByTimeAsync(187);
    expect(curves.play).not.toHaveBeenCalled(); // Android replaces a running buzz: each piece starts at its own time, not before
    await vi.advanceTimersByTimeAsync(1);
    expect(curves.play).toHaveBeenCalledTimes(1);
    expect(curves.play.mock.calls[0][0].taps.map((tap: { at: number }) => tap.at)).toEqual([0]);
    expect(curves.play.mock.calls[0][0].ramps).toEqual([]);
    await vi.advanceTimersByTimeAsync(600 - 188);
    expect(curves.play).toHaveBeenCalledTimes(2);
    const { taps, ramps } = curves.play.mock.calls[1][0];
    expect(taps.map((tap: { at: number }) => tap.at)).toEqual([700, 810]); // the landing's success: a tap, then a stronger one
    expect(taps[1].intensity).toBeGreaterThan(taps[0].intensity);
    expect(ramps).toHaveLength(1);
    expect(ramps[0].at).toBe(0);
    expect(ramps[0].duration).toBe(700);
    expect(ramps[0].to.intensity).toBeGreaterThan(ramps[0].from.intensity);
    expect(ramps[0].to.sharpness).toBeGreaterThan(ramps[0].from.sharpness);
    await vi.advanceTimersByTimeAsync(3000);
    expect(plugin.impact).not.toHaveBeenCalled(); // no ladder on top
  });

  it("stops the flight when its suggestion is answered on the way", async () => {
    let chip = true;
    playScore(beat.map((pulse) => ({ ...pulse, still: () => chip })), "suggestion", [{ ...flight, still: () => chip }]);
    await vi.advanceTimersByTimeAsync(700);
    chip = false;
    await vi.advanceTimersByTimeAsync(100);
    expect(curves.stop).toHaveBeenCalledWith({ id: "p1" });
  });

  it("never starts the flight of a suggestion dismissed between its light-up and its launch", async () => {
    let chip = true;
    playScore(beat.map((pulse) => ({ ...pulse, still: () => chip })), "suggestion", [{ ...flight, still: () => chip }]);
    await vi.advanceTimersByTimeAsync(250);
    chip = false; // "not this"
    await vi.advanceTimersByTimeAsync(2000);
    expect(curves.play).toHaveBeenCalledTimes(1); // the light-up only
    expect(curves.play.mock.calls[0][0].ramps).toEqual([]); // the flight was never sent to the phone
  });

  it("keeps asking during a flight even when it has no ladder ticks to ask at", async () => {
    let chip = true;
    playScore([{ at: 0, level: "light" }], "suggestion", [{ at: 100, duration: 700, from: "light", to: "heavy", still: () => chip }]);
    await vi.advanceTimersByTimeAsync(350);
    chip = false;
    await vi.advanceTimersByTimeAsync(RAMP_CHECK);
    expect(curves.stop).toHaveBeenCalledTimes(1);
  });

  it("stops a playing curve before a person's moment, so the curve's later stop can't cut the moment", async () => {
    let chip = true;
    playScore(beat.map((pulse) => ({ ...pulse, still: () => chip })), "suggestion", [{ ...flight, still: () => chip }]);
    await vi.advanceTimersByTimeAsync(1010);
    chip = false;
    haptic("filled"); // accepted mid-flight
    await vi.advanceTimersByTimeAsync(0);
    expect(curves.stop).toHaveBeenCalledTimes(1);
    expect(curves.stop.mock.invocationCallOrder[0]).toBeLessThan(plugin.notification.mock.invocationCallOrder[0]);
    await vi.advanceTimersByTimeAsync(1000); // the flight's own checks now say no
    expect(curves.stop).toHaveBeenCalledTimes(1); // and never reach the phone again
  });

  it("stops when told", async () => {
    const stop = playScore(beat, "suggestion", [flight]);
    await vi.advanceTimersByTimeAsync(200);
    stop();
    await vi.advanceTimersByTimeAsync(0);
    expect(curves.stop).toHaveBeenCalledWith({ id: "p1" });
  });

  it("keeps moments in order and apart while a curve's stop is still on its way", async () => {
    let stopped = () => {};
    curves.stop.mockImplementation(() => new Promise<void>((resolve) => { stopped = resolve; }));
    playScore(beat, "suggestion", [flight]);
    await vi.advanceTimersByTimeAsync(700); // mid-flight
    haptic("filled");
    haptic("filled");
    await vi.advanceTimersByTimeAsync(100);
    expect(plugin.notification).not.toHaveBeenCalled(); // nothing may start before the stop has gone through
    playScore([{ at: 0, level: "heavy" }], "suggestion"); // nor a new curve
    await vi.advanceTimersByTimeAsync(0);
    expect(curves.play).toHaveBeenCalledTimes(2); // only the light-up and the flight, from before
    const released = Date.now();
    stopped();
    await vi.advanceTimersByTimeAsync(1000);
    expect(plugin.notification).toHaveBeenCalledTimes(2);
    expect(at.notifications[0]).toBeGreaterThanOrEqual(released);
    expect(at.notifications[1] - at.notifications[0]).toBeGreaterThanOrEqual(130 + 60); // the second waits for the first, from when it really started
  });

  it("gives way to a moment that is buzzing: a piece that would start then is played as the ladder, pulse by pulse", async () => {
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    haptic("logged");
    playScore([{ at: 0, level: "heavy" }, { at: 900, level: "light" }], "suggestion");
    await vi.advanceTimersByTimeAsync(1000);
    stop();
    expect(heard).toContain("suggestion heavy: dropped (another buzz was still running)");
    expect(curves.play).toHaveBeenCalledTimes(1); // the later piece, once the moment was done
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
