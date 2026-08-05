import { Capacitor } from "@capacitor/core";

// The native shell's webview doesn't share an origin with the API the way
// the web PWA does, so relative fetches like "/dex" resolve against nothing.
// Web stays relative (same-origin, works through the vite dev proxy too).
export const API_BASE = Capacitor.isNativePlatform() ? "https://app.nammaindies.org" : "";
