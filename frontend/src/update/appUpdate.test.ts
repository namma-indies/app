import { describe, expect, it, vi } from "vitest";
import {
  CHECK_INTERVAL_MS,
  MIN_CHECK_GAP_MS,
  createUpdateController,
  scheduleUpdateChecks,
  watchForUpdate,
  type CheckerEnv,
} from "./appUpdate";
import { holdReload, isBusy, onBusyChange } from "./reloadGuard";

function controllerHarness(opts: { busy?: boolean; hidden?: boolean } = {}) {
  const state = { busy: !!opts.busy, hidden: !!opts.hidden };
  let busyCb = () => {};
  const reload = vi.fn();
  const setPrompt = vi.fn();
  const c = createUpdateController({
    isBusy: () => state.busy,
    onBusyChange: (cb) => {
      busyCb = cb;
      return () => {};
    },
    reload,
    setPrompt,
    isHidden: () => state.hidden,
  });
  return { c, state, reload, setPrompt, fireBusy: () => busyCb() };
}

describe("createUpdateController", () => {
  it("reloads straight away when nothing is in progress", () => {
    const h = controllerHarness();
    h.c.updateReady();
    expect(h.reload).toHaveBeenCalledTimes(1);
    expect(h.setPrompt).not.toHaveBeenCalled();
  });

  it("shows the prompt instead of reloading while work is in progress", () => {
    const h = controllerHarness({ busy: true });
    h.c.updateReady();
    expect(h.reload).not.toHaveBeenCalled();
    expect(h.setPrompt).toHaveBeenCalledWith(true);
  });

  it("reloads when the user taps Reload on the prompt, even while busy", () => {
    const h = controllerHarness({ busy: true });
    h.c.updateReady();
    h.c.reloadNow();
    expect(h.reload).toHaveBeenCalledTimes(1);
  });

  it("never reloads twice", () => {
    const h = controllerHarness();
    h.c.updateReady();
    h.c.reloadNow();
    h.c.updateReady();
    expect(h.reload).toHaveBeenCalledTimes(1);
  });

  it("takes a pending update once work ends and the page is in the background", () => {
    const h = controllerHarness({ busy: true });
    h.c.updateReady();
    h.state.busy = false;
    h.fireBusy(); // work ended but the page is in view: leave the prompt
    expect(h.reload).not.toHaveBeenCalled();
    h.state.hidden = true;
    h.c.visibilityChanged();
    expect(h.reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload a backgrounded page that is still mid-work", () => {
    const h = controllerHarness({ busy: true, hidden: true });
    h.c.updateReady();
    h.c.visibilityChanged();
    expect(h.reload).not.toHaveBeenCalled();
  });

  it("does nothing on visibility changes when no update is ready", () => {
    const h = controllerHarness({ hidden: true });
    h.c.visibilityChanged();
    h.fireBusy();
    expect(h.reload).not.toHaveBeenCalled();
  });
});

describe("reloadGuard", () => {
  it("is busy while any hold is open and notifies on change", () => {
    const cb = vi.fn();
    const off = onBusyChange(cb);
    const a = holdReload();
    const b = holdReload();
    expect(isBusy()).toBe(true);
    a();
    expect(isBusy()).toBe(true);
    b();
    b(); // releasing twice is harmless
    expect(isBusy()).toBe(false);
    expect(cb).toHaveBeenCalledTimes(4);
    off();
  });
});

function fakeEnv() {
  let t = 0;
  let visible = true;
  const timers: { cb: () => void; ms: number }[] = [];
  const visCbs: (() => void)[] = [];
  const onlineCbs: (() => void)[] = [];
  const env: CheckerEnv = {
    now: () => t,
    setInterval: (cb, ms) => timers.push({ cb, ms }) - 1,
    clearInterval: vi.fn(),
    isVisible: () => visible,
    onVisible: (cb) => (visCbs.push(cb), () => {}),
    onOnline: (cb) => (onlineCbs.push(cb), () => {}),
  };
  return {
    env,
    advance: (ms: number) => (t += ms),
    setVisible: (v: boolean) => (visible = v),
    timers,
    fireVisible: () => visCbs.forEach((cb) => cb()),
    fireOnline: () => onlineCbs.forEach((cb) => cb()),
  };
}

describe("scheduleUpdateChecks", () => {
  it("checks on an interval", () => {
    const f = fakeEnv();
    const update = vi.fn().mockResolvedValue(undefined);
    scheduleUpdateChecks({ update }, f.env);
    expect(f.timers[0].ms).toBe(CHECK_INTERVAL_MS);
    f.timers[0].cb();
    expect(update).toHaveBeenCalledTimes(1);
  });

  it("checks when the app is resumed, but not in a storm", () => {
    const f = fakeEnv();
    const update = vi.fn().mockResolvedValue(undefined);
    scheduleUpdateChecks({ update }, f.env);
    f.fireVisible(); // too soon after registration
    expect(update).not.toHaveBeenCalled();
    f.advance(MIN_CHECK_GAP_MS);
    f.fireVisible();
    f.fireVisible();
    expect(update).toHaveBeenCalledTimes(1);
  });

  it("does not check when the page goes to the background", () => {
    const f = fakeEnv();
    const update = vi.fn().mockResolvedValue(undefined);
    scheduleUpdateChecks({ update }, f.env);
    f.advance(MIN_CHECK_GAP_MS);
    f.setVisible(false);
    f.fireVisible();
    expect(update).not.toHaveBeenCalled();
  });

  it("checks when the network comes back, and survives a failed check", () => {
    const f = fakeEnv();
    const update = vi.fn().mockRejectedValue(new Error("offline"));
    scheduleUpdateChecks({ update }, f.env);
    f.advance(MIN_CHECK_GAP_MS);
    f.fireOnline();
    expect(update).toHaveBeenCalledTimes(1);
  });
});

function swFixture(controller: object | null, active: object | null) {
  const target = (l: Record<string, () => void>) => ({
    addEventListener: (type: string, cb: () => void) => {
      l[type] = cb;
    },
  });
  const cl: Record<string, () => void> = {};
  const rl: Record<string, () => void> = {};
  return {
    container: { controller, ...target(cl) } as unknown as ServiceWorkerContainer,
    reg: { active, ...target(rl) } as unknown as ServiceWorkerRegistration,
    cl,
    rl,
  };
}

describe("watchForUpdate", () => {
  it("fires when a new worker takes over a controlled page", () => {
    const f = swFixture({}, {});
    const onUpdate = vi.fn();
    watchForUpdate(f.container, f.reg, onUpdate);
    f.cl.controllerchange();
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it("stays quiet on the first install (nothing stale to replace)", () => {
    const f = swFixture(null, null);
    const onUpdate = vi.fn();
    watchForUpdate(f.container, f.reg, onUpdate);
    f.rl.updatefound();
    f.cl.controllerchange();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("fires for an update found from an uncontrolled page (hard reload)", () => {
    const f = swFixture(null, {});
    const onUpdate = vi.fn();
    watchForUpdate(f.container, f.reg, onUpdate);
    f.rl.updatefound();
    f.cl.controllerchange();
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });
});
