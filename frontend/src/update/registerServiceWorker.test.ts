import { afterEach, describe, expect, it, vi } from "vitest";
import { startServiceWorker } from "./registerServiceWorker";

vi.mock("@capacitor/core", () => ({ Capacitor: { isNativePlatform: () => false } }));

const stops: (() => void)[] = [];
afterEach(() => {
  stops.splice(0).forEach((stop) => stop());
  vi.unstubAllGlobals();
});

function fixture(readyState = "complete", controlled = true) {
  const oldWorker = controlled ? {} : null;
  const reg = Object.assign(new EventTarget(), {
    active: oldWorker,
    installing: null as object | null,
    update: vi.fn().mockResolvedValue(undefined),
  });
  const container = Object.assign(new EventTarget(), {
    controller: oldWorker,
    register: vi.fn().mockResolvedValue(reg),
  });
  const reload = vi.fn();
  const win = Object.assign(new EventTarget(), {
    location: { reload }, setInterval: vi.fn(() => 1), clearInterval: vi.fn(),
  });
  const doc = Object.assign(new EventTarget(), { readyState, visibilityState: "visible" });
  vi.stubGlobal("window", win);
  vi.stubGlobal("document", doc);
  vi.stubGlobal("navigator", { serviceWorker: container });
  return {
    reg, container, win, reload,
    start() {
      const handle = startServiceWorker(vi.fn());
      stops.push(handle.stop);
      return handle;
    },
    takeControl(notify = true) {
      container.controller = {};
      reg.active = container.controller;
      if (notify) container.dispatchEvent(new Event("controllerchange"));
    },
  };
}

async function settled() {
  await Promise.resolve();
  await Promise.resolve();
}

describe("service-worker startup (#175)", () => {
  it("observes takeover before window.load without waiting to register", async () => {
    const f = fixture("loading");
    f.start();
    f.takeControl();
    expect(f.container.register).not.toHaveBeenCalled();
    expect(f.reload).toHaveBeenCalledTimes(1);
    f.win.dispatchEvent(new Event("load"));
    await settled();
    expect(f.container.register).toHaveBeenCalledWith("/sw.js", { scope: "/", updateViaCache: "none" });
    expect(f.reload).toHaveBeenCalledTimes(1);
  });

  it.each([true, false])("observes takeover before registration resolves (event dispatched: %s)", async (notify) => {
    const f = fixture();
    let finish!: (reg: typeof f.reg) => void;
    f.container.register.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    f.start();
    f.takeControl(notify);
    if (notify) expect(f.reload).toHaveBeenCalledTimes(1);
    finish(f.reg);
    await settled();
    expect(f.reload).toHaveBeenCalledTimes(1);
    expect(f.reg.update).toHaveBeenCalledTimes(1);
    f.container.dispatchEvent(new Event("controllerchange"));
    expect(f.reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload on first install, but observes the following update", async () => {
    const f = fixture("complete", false);
    f.start();
    await settled();
    f.takeControl();
    expect(f.reload).not.toHaveBeenCalled();
    f.reg.dispatchEvent(new Event("updatefound"));
    f.takeControl();
    expect(f.reload).toHaveBeenCalledTimes(1);
  });

  it("stops before load without registering or observing a later takeover", () => {
    const f = fixture("loading");
    f.start().stop();
    f.win.dispatchEvent(new Event("load"));
    f.takeControl();
    expect(f.container.register).not.toHaveBeenCalled();
    expect(f.reload).not.toHaveBeenCalled();
  });

  it("does not resume a stopped registration when its promise resolves", async () => {
    const f = fixture();
    let finish!: (reg: typeof f.reg) => void;
    f.container.register.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    f.start().stop();
    finish(f.reg);
    await settled();
    f.takeControl();
    expect(f.reg.update).not.toHaveBeenCalled();
    expect(f.win.setInterval).not.toHaveBeenCalled();
    expect(f.reload).not.toHaveBeenCalled();
  });
});
