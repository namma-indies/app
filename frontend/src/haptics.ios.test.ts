// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const platform = vi.hoisted(() => ({ native: false }));
vi.mock("@capacitor/haptics", () => ({
  Haptics: { impact: vi.fn(), notification: vi.fn() },
  ImpactStyle: { Light: "LIGHT", Medium: "MEDIUM" },
  NotificationType: { Success: "SUCCESS" },
}));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => platform.native, isPluginAvailable: () => platform.native },
}));

import { haptic, hapticChannel, onHapticOutcome, switchSupported } from "./haptics";

const IPHONE_18 = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 Version/18.6 Mobile/15E148 Safari/604.1";
const IPHONE_17 = IPHONE_18.replace("18_6", "17_5").replace("18.6", "17.5");
let epoch = 0;
let ticks = 0;
const phone = (over: Record<string, unknown> = {}) => vi.stubGlobal("navigator", { userAgent: IPHONE_18, maxTouchPoints: 5, userActivation: { isActive: true, hasBeenActive: true }, ...over });

// jsdom has no haptics: count the switch being flipped.
document.addEventListener("change", () => { ticks++; });

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.now() + (epoch += 1_000_000));
  platform.native = false;
  ticks = 0;
  document.body.innerHTML = "";
  vi.stubGlobal("CSS", { supports: (property: string, value: string) => property === "appearance" && value === "switch" });
  phone();
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("the iPhone browser tick", () => {
  it("is offered on iOS 18+ web, hidden from assistive tech, and changes no page data", () => {
    expect(switchSupported()).toBe(true);
    expect(hapticChannel()).toBe("switch");
    haptic("tick");
    const label = document.querySelector("label[aria-hidden='true']")!;
    expect(label.querySelector("input[type=checkbox][switch]")).not.toBeNull();
    expect(document.querySelectorAll("form")).toHaveLength(0);
    expect(ticks).toBe(1);
  });

  it("ticks once per step of a moment, in step with the native timing", () => {
    haptic("logged");
    expect(ticks).toBe(1);
    vi.advanceTimersByTime(160);
    expect(ticks).toBe(2);
    vi.advanceTimersByTime(120);
    expect(ticks).toBe(3);
  });

  it("stays silent when no tap just happened (confetti, comet landings), and says why", () => {
    phone({ userActivation: { isActive: false, hasBeenActive: true } });
    const heard: string[] = [];
    const stop = onHapticOutcome((outcome) => heard.push(outcome));
    haptic("tick");
    stop();
    expect(ticks).toBe(0);
    expect(heard[0]).toMatch(/^tick: silent \(iPhone browsers only tick for a tap/);
  });

  it("is not offered before iOS 18, off iOS, or in the native app", () => {
    phone({ userAgent: IPHONE_17 });
    expect(switchSupported()).toBe(false);
    phone({ userAgent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/120", maxTouchPoints: 0 });
    expect(switchSupported()).toBe(false);
    phone();
    vi.stubGlobal("CSS", { supports: () => false });
    expect(switchSupported()).toBe(false);
    vi.stubGlobal("CSS", { supports: () => true });
    platform.native = true;
    expect(switchSupported()).toBe(false);
    expect(hapticChannel()).toBe("native");
  });

  it("prefers navigator.vibrate where a browser has it", () => {
    phone({ vibrate: vi.fn() });
    expect(hapticChannel()).toBe("vibrate");
  });
});
