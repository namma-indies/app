import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Namma IndieDex — lean placeholder PWA. A designer will rebuild this later.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      workbox: {
        // Server-owned routes must reach the network, not the cached SPA shell.
        // Without this the SW's navigateFallback serves index.html for the
        // magic-link consume redirect and the /join gate, swallowing the
        // Set-Cookie (login silently fails) and hiding the server-rendered
        // /join page. Let these navigations pass through to the backend.
        navigateFallbackDenylist: [/^\/auth\//, /^\/join/],
      },
      manifest: {
        name: "indiedex — Namma Indies",
        short_name: "indiedex",
        description: "Spot and log indie street dogs around you.",
        start_url: "/",
        display: "standalone",
        background_color: "#faf7f2",
        theme_color: "#a5502e",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
        ],
      },
    }),
  ],
  server: {
    proxy: {
      // Every path api.ts calls has to be listed here, or `npm run dev` serves
      // the SPA shell for it and the call 404s. Production never shows this --
      // Caddy fronts the API and the built assets on one origin -- so a missing
      // entry only ever costs a developer an afternoon.
      "/sighting": { target: "http://localhost:8000", changeOrigin: true },
      "/dex": { target: "http://localhost:8000", changeOrigin: true },
      "/map": { target: "http://localhost:8000", changeOrigin: true },
      "/photo": { target: "http://localhost:8000", changeOrigin: true },
      "/proposal": { target: "http://localhost:8000", changeOrigin: true },
      "/auth": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
