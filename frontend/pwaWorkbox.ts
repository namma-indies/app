import type { VitePWAOptions } from "vite-plugin-pwa";
import { SERVER_ROUTES } from "./src/serverRoutes";

// Own module so a test can assert on it (vite.config.ts has side effects).
export const workboxOptions: NonNullable<VitePWAOptions["workbox"]> = {
  // Explicit, not implied by registerType: with injectRegister false the
  // plugin leaves both off, and our registrar never sends SKIP_WAITING, so a
  // new worker would wait forever behind any open tab (#175). src/update/
  // handles the page side once the new worker takes over.
  skipWaiting: true,
  clientsClaim: true,
  cleanupOutdatedCaches: true,
  // Server-owned routes must reach the network, not the cached SPA shell
  // (src/serverRoutes.ts says which, and why).
  navigateFallbackDenylist: SERVER_ROUTES,
};
