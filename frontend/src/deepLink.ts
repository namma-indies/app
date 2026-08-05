import { Capacitor } from "@capacitor/core";
import { API_BASE } from "./apiBase";

/**
 * Universal Links (magic-link emails) land here on iOS instead of Safari.
 * The OS hands the app the URL directly -- nothing has hit the server yet,
 * so the app performs the consume request itself, then tells the caller
 * to re-check auth state.
 */
export function listenForAuthLinks(onConsumed: () => void): void {
  if (!Capacitor.isNativePlatform()) return;

  import("@capacitor/app").then(({ App }) => {
    App.addListener("appUrlOpen", async ({ url }: { url: string }) => {
      const { pathname, search } = new URL(url);
      if (!pathname.startsWith("/auth/")) return;
      await fetch(`${API_BASE}${pathname}${search}`, { credentials: "include" }).catch(() => {});
      onConsumed();
    });
  });
}
