/** First-party error signals only. Never send the event, message, stack or URL. */
export function startErrorMonitoring(enabled: boolean): () => void {
  if (!enabled) return () => {};
  let lastSent = -Infinity;
  const report = (kind: "error" | "unhandled_rejection") => {
    const now = performance.now();
    if (now - lastSent < 60_000 || !navigator.onLine) return;
    lastSent = now;
    // Same-origin cookies supply auth. Failures are swallowed, not retried or
    // queued offline: monitoring must not create a rejection loop or new data.
    try {
      void fetch("/monitoring/client-error", {
        method: "POST",
        credentials: "same-origin",
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
