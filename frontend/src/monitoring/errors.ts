import { API_BASE } from "../apiBase";

/** First-party error signals only. Never send the event, message, stack or URL. */
export function startErrorMonitoring(enabled: boolean): () => void {
  if (!enabled) return () => {};
  let lastSent = -Infinity;
  const report = (kind: "error" | "unhandled_rejection") => {
    const now = performance.now();
    if (now - lastSent < 60_000 || !navigator.onLine) return;
    lastSent = now;
    // The session cookie supplies auth. API_BASE is empty on the web and the
    // API origin in native (Capacitor) builds, where a relative path would hit
    // the bundled app instead -- hence "include", as every other API call uses.
    // Failures are swallowed, not retried or queued offline: monitoring must
    // not create a rejection loop or new data.
    try {
      void fetch(`${API_BASE}/monitoring/client-error`, {
        method: "POST",
        credentials: "include",
        referrerPolicy: "no-referrer",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind }),
        keepalive: true,
      }).catch(() => {});
    } catch { /* An unavailable fetch is also best effort. */ }
  };
  const error = () => report("error");
  const rejection = () => report("unhandled_rejection");
  window.addEventListener("error", error);
  window.addEventListener("unhandledrejection", rejection);
  return () => {
    window.removeEventListener("error", error);
    window.removeEventListener("unhandledrejection", rejection);
  };
}
