import { Capacitor } from "@capacitor/core";
import {
  createUpdateController,
  scheduleUpdateChecks,
  watchForUpdate,
} from "./appUpdate";
import { isBusy, onBusyChange } from "./reloadGuard";

/** The native apps bundle the frontend, so a service worker there only adds a stale second cache. */
async function removeServiceWorkers(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  const regs = await navigator.serviceWorker.getRegistrations();
  await Promise.all(regs.map((r) => r.unregister()));
  if ("caches" in window) {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
  }
}

/**
 * Registers the service worker and keeps the page current. `setPrompt` drives
 * the "New version ready" banner.
 */
export function startServiceWorker(setPrompt: (visible: boolean) => void): {
  reloadNow: () => void;
  stop: () => void;
} {
  const inert = { reloadNow: () => window.location.reload(), stop: () => {} };
  if (Capacitor.isNativePlatform()) {
    removeServiceWorkers().catch(() => {});
    return inert;
  }
  if (!("serviceWorker" in navigator)) return inert;

  const controller = createUpdateController({
    isBusy,
    onBusyChange,
    reload: () => window.location.reload(),
    setPrompt,
    isHidden: () => document.visibilityState === "hidden",
  });
  const onVisibility = () => controller.visibilityChanged();
  document.addEventListener("visibilitychange", onVisibility);

  let stopChecks = () => {};
  let stopped = false;
  // Wait for load like the stock register script did: the SW's precache fetch
  // should not compete with the first paint.
  const register = () =>
    navigator.serviceWorker
      // updateViaCache "none": sw.js and anything it imports are always
      // revalidated, whatever Cache-Control a proxy or older server sent.
      .register("/sw.js", { scope: "/", updateViaCache: "none" })
      .then((reg) => {
        if (stopped) return;
        watchForUpdate(navigator.serviceWorker, reg, controller.updateReady);
        stopChecks = scheduleUpdateChecks(reg, {
          now: () => Date.now(),
          setInterval: (cb, ms) => window.setInterval(cb, ms),
          clearInterval: (id) => window.clearInterval(id as number),
          isVisible: () => document.visibilityState === "visible",
          onVisible: (cb) => {
            document.addEventListener("visibilitychange", cb);
            return () => document.removeEventListener("visibilitychange", cb);
          },
          onOnline: (cb) => {
            window.addEventListener("online", cb);
            return () => window.removeEventListener("online", cb);
          },
        });
        // The registration call only checks the server when the browser decides
        // to; ask now so a page opened from a stale precache finds out at once.
        reg.update().catch(() => {});
      })
      .catch(() => {});
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });

  return {
    reloadNow: controller.reloadNow,
    stop: () => {
      stopped = true;
      stopChecks();
      controller.dispose();
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
