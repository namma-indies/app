// Keeps a long-lived page on the current build.
//
// The service worker (skipWaiting + clientsClaim) swaps itself in on its own,
// but the *page* keeps running the JS it already loaded. This module notices
// the swap, then reloads the page when that loses nothing, or asks first when
// it would. It also makes the page go looking for new versions: the browser
// only checks on navigation, and an installed PWA is resumed, not navigated.

export const CHECK_INTERVAL_MS = 30 * 60 * 1000;
/** Floor between resume/online-triggered checks, so tab flicker is not a request storm. */
export const MIN_CHECK_GAP_MS = 60 * 1000;

export type UpdateDeps = {
  isBusy: () => boolean;
  onBusyChange: (cb: () => void) => () => void;
  reload: () => void;
  /** Show or hide the "New version ready" prompt. */
  setPrompt: (visible: boolean) => void;
  isHidden: () => boolean;
  /** Run later, after the current synchronous work (default: setTimeout 0). */
  defer?: (cb: () => void) => void;
};

export type UpdateController = {
  /** A new version has taken over the service worker. */
  updateReady: () => void;
  /** The user tapped "Reload" on the prompt. */
  reloadNow: () => void;
  /** The page went to the background or came back. */
  visibilityChanged: () => void;
  dispose: () => void;
};

export function createUpdateController(deps: UpdateDeps): UpdateController {
  let ready = false;
  let reloading = false;

  function reload() {
    if (reloading) return;
    reloading = true;
    deps.reload();
  }

  function evaluate() {
    if (!ready || reloading) return;
    if (!deps.isBusy()) {
      reload();
    } else {
      deps.setPrompt(true);
    }
  }

  const defer = deps.defer ?? ((cb: () => void) => void setTimeout(cb, 0));
  const stopWatching = deps.onBusyChange(() => {
    // Work finished while the prompt was up: take the update when the page is
    // out of sight (invisible), otherwise leave the prompt for the user.
    // Re-check after the change settles: one React commit can release a hold
    // (recording stops) before the next mounts (the unsaved note), and the
    // gap between them looks idle.
    defer(() => {
      if (ready && !deps.isBusy() && deps.isHidden()) reload();
    });
  });

  return {
    updateReady() {
      ready = true;
      evaluate();
    },
    reloadNow: reload,
    visibilityChanged() {
      if (ready && deps.isHidden() && !deps.isBusy()) reload();
    },
    dispose: stopWatching,
  };
}

type Registration = Pick<ServiceWorkerRegistration,
  "update" | "active" | "installing" | "addEventListener" | "removeEventListener">;

export type CheckerEnv = {
  now: () => number;
  setInterval: (cb: () => void, ms: number) => unknown;
  clearInterval: (id: unknown) => void;
  isVisible: () => boolean;
  onVisible: (cb: () => void) => () => void;
  onOnline: (cb: () => void) => () => void;
};

/**
 * Ask the server for a new sw.js on a timer, when the app is resumed, and when
 * the network comes back. Returns a stop function.
 */
export function scheduleUpdateChecks(reg: Pick<Registration, "update">, env: CheckerEnv): () => void {
  let last = env.now();
  function check(throttle: boolean) {
    const t = env.now();
    if (throttle && t - last < MIN_CHECK_GAP_MS) return;
    last = t;
    // Offline or a transient failure: the next trigger tries again.
    reg.update().catch(() => {});
  }
  const timer = env.setInterval(() => check(false), CHECK_INTERVAL_MS);
  const offVisible = env.onVisible(() => {
    if (env.isVisible()) check(true);
  });
  const offOnline = env.onOnline(() => check(true));
  return () => {
    env.clearInterval(timer);
    offVisible();
    offOnline();
  };
}

/**
 * Calls `onUpdate` when a new service worker takes control of a page that was
 * already running an older one -- not on the very first install, where there
 * is nothing stale to replace.
 */
export function watchForUpdate(
  container: Pick<ServiceWorkerContainer, "controller" | "addEventListener" | "removeEventListener">,
  onUpdate: () => void,
) {
  let previous = container.controller;
  let replacing = !!previous;
  let stopRegistration = () => {};

  function reconcile() {
    const current = container.controller;
    if (current === previous) return;
    previous = current;
    if (!current) return;
    if (replacing) onUpdate();
    replacing = true;
  }
  // Observe immediately, before load or register() can yield to a takeover.
  container.addEventListener("controllerchange", reconcile);

  return {
    observeRegistration(reg: Pick<Registration,
      "active" | "installing" | "addEventListener" | "removeEventListener">) {
      stopRegistration();
      const updateFound = () => {
        // A hard reload can leave the page uncontrolled with an active worker.
        if (reg.active) replacing = true;
      };
      reg.addEventListener("updatefound", updateFound);
      stopRegistration = () => reg.removeEventListener("updatefound", updateFound);
      if (reg.installing) updateFound();
      // The controller may already have changed while registration resolved,
      // before the browser's queued controllerchange event is dispatched.
      reconcile();
    },
    stop() {
      container.removeEventListener("controllerchange", reconcile);
      stopRegistration();
    },
  };
}
