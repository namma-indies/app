import { useEffect } from "react";

// Screens that hold work the user would lose to a page reload (a capture draft,
// a recording, an open sheet) register a hold while they do. The update logic
// asks isBusy() before reloading and shows a prompt instead when it is true.
const holds = new Set<symbol>();
const listeners = new Set<() => void>();

function notify() {
  listeners.forEach((cb) => cb());
}

export function holdReload(): () => void {
  const id = Symbol("reload-hold");
  holds.add(id);
  notify();
  return () => {
    if (holds.delete(id)) notify();
  };
}

export function isBusy(): boolean {
  return holds.size > 0;
}

/** Called whenever the busy state changes. Returns an unsubscribe. */
export function onBusyChange(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** Hold reloads for as long as `active` is true (and the component is mounted). */
export function useReloadGuard(active: boolean = true): void {
  useEffect(() => {
    if (!active) return;
    return holdReload();
  }, [active]);
}
