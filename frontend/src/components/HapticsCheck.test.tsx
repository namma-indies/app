// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

const plugin = vi.hoisted(() => ({ impact: vi.fn(), notification: vi.fn() }));
const platform = vi.hoisted(() => ({ native: true }));
vi.mock("@capacitor/haptics", () => ({
  Haptics: plugin,
  ImpactStyle: { Light: "LIGHT", Medium: "MEDIUM" },
  NotificationType: { Success: "SUCCESS" },
}));
vi.mock("@capacitor/core", () => ({
  Capacitor: {
    isNativePlatform: () => platform.native,
    isPluginAvailable: () => platform.native,
    getPlatform: () => (platform.native ? "ios" : "web"),
  },
}));
vi.mock("@capacitor/app", () => ({ App: { getInfo: async () => ({ version: "1.4.0", build: "29" }) } }));

import HapticsCheck, { wantsHapticsCheck } from "./HapticsCheck";

let epoch = 0;
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(Date.now() + (epoch += 1_000_000));
  platform.native = true;
  plugin.impact.mockResolvedValue(undefined);
  plugin.notification.mockResolvedValue(undefined);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("the Haptics check", () => {
  it("opens only for ?haptics=check", () => {
    expect(wantsHapticsCheck("?haptics=check")).toBe(true);
    expect(wantsHapticsCheck("?foo=1&haptics=check")).toBe(true);
    expect(wantsHapticsCheck("?haptics=1")).toBe(false);
    expect(wantsHapticsCheck("")).toBe(false);
  });

  it("shows the platform, plugin, vibrate, activation, switch and the app build", async () => {
    vi.stubGlobal("navigator", { userAgent: "x", userActivation: { hasBeenActive: true, isActive: false } });
    render(<HapticsCheck onClose={() => {}} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const text = document.body.textContent!;
    for (const expected of ["ios (app)", "Native haptics plugin", "navigator.vibrate", "User activation (page has been tapped)", "iOS switch trick", "1.4.0 (build 29)", "native"]) {
      expect(text).toContain(expected);
    }
  });

  it("plays each named moment and says what happened", async () => {
    render(<HapticsCheck onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "tick" }));
    fireEvent.click(screen.getByRole("button", { name: "filled" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(plugin.impact).toHaveBeenCalledWith({ style: "LIGHT" });
    expect(plugin.notification).toHaveBeenCalledWith({ type: "SUCCESS" });
    expect(document.querySelector(".haptics-log")!.textContent).toContain("tick native: ok");
    for (const moment of ["tick", "filled", "logged", "recordStart", "recordStop"]) {
      expect(screen.getByRole("button", { name: moment })).toBeTruthy();
    }
  });

  it("shows a native failure instead of hiding it", async () => {
    plugin.impact.mockRejectedValue(new Error("UNIMPLEMENTED"));
    render(<HapticsCheck onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "tick" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(document.querySelector(".haptics-log")!.textContent).toContain("tick native: failed (UNIMPLEMENTED)");
  });

  it("plays the delayed variant after 1.5 s, and closes", async () => {
    const onClose = vi.fn();
    render(<HapticsCheck onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "tick later" }));
    expect(plugin.impact).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1600); });
    expect(plugin.impact).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });
});
