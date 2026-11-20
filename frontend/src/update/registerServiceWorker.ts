import { Capacitor } from "@capacitor/core";
import {
  createUpdateController,
  scheduleUpdateChecks,
  watchForUpdate,
} from "./appUpdate";
import { isBusy, onBusyChange } from "./reloadGuard";

/**
 * The native apps bundle the frontend, so a service worker there only adds a
 * stale second cache. Runs on every native launch.
 *
 * Deletes only the service worker's own caches (Workbox names them
 * `workbox-*`; pwaWorkbox.ts configures no runtime caches with other names).
 * Any other CacheStorage entry the app adds later is left alone rather than
 * silently wiped on each launch.
 */
export const SERVICE_WORKER_CACHE_PREFIX = "workbox-";

export async function removeServiceWorkers(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  const regs = await navigator.serviceWorker.getRegistrations();
  await Promise.all(regs.map((r) => r.unregister()));
  if ("caches" in window) {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith(SERVICE_WORKER_CACHE_PREFIX)).map((k) => caches.delete(k)));
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
  const watcher = watchForUpdate(navigator.serviceWorker, controller.updateReady);
  const onVisibility = () => controller.visibilityChanged();
  document.addEventListener("visibilitychange", onVisibility);

  let stopChecks = () => {};
  let stopped = false;
  // Wait for load like the stock register script did: the SW's precache fetch
  // should not compete with the first paint.
  const register = () => {
    if (stopped) return;
    return navigator.serviceWorker
      // updateViaCache "none": sw.js and anything it imports are always
      // revalidated, whatever Cache-Control a proxy or older server sent.
      .register("/sw.js", { scope: "/", updateViaCache: "none" })
      .then((reg) => {
        if (stopped) return;
        watcher.observeRegistration(reg);
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
  };
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });

  return {
    reloadNow: controller.reloadNow,
    stop: () => {
      stopped = true;
      window.removeEventListener("load", register);
      watcher.stop();
      stopChecks();
      controller.dispose();
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
