import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import fs from "node:fs";

// Namma IndieDex — lean placeholder PWA. A designer will rebuild this later.

// Dev-server wiring. Overridable because none of these are universal: port 8000
// is already taken on some machines, and the bucket name differs per developer.
//   VITE_API_TARGET=http://localhost:8300 npm run dev
const API = process.env.VITE_API_TARGET ?? API;
const S3 = process.env.VITE_S3_TARGET ?? "http://localhost:9000";
const BUCKET = process.env.VITE_S3_BUCKET ?? "indiedex-dev";
const CERT = { cert: ".certs/dev.crt", key: ".certs/dev.key" };
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
    // Reachable from a phone on the LAN, not just this machine. This app is
    // used outdoors on a handset -- testing only in a desktop browser misses
    // the camera, the GPS fix and the offline queue, which is most of it.
    host: true,

    // Pinned rather than left to vite's first-free-port scan. The backend signs
    // photo URLs and magic links against PUBLIC_BASE_URL / S3_PUBLIC_ENDPOINT,
    // so a dev server that quietly lands on 5173 breaks every image and every
    // sign-in link with no visible cause. strictPort makes the collision loud.
    port: 5174,
    strictPort: true,

    // HTTPS whenever local certs are present, and it is not a nicety. The
    // session cookie is Secure + SameSite=None (the iOS webview is genuinely
    // cross-origin), so over plain http a browser declines to store it and
    // sign-in fails with no error anywhere -- the app just returns to the gate.
    // Camera and geolocation also need a secure context on a non-localhost
    // origin, so LAN testing cannot work over http at all.
    //
    // Absent certs fall back to http so a fresh clone still runs. Generate a
    // pair with any self-signed recipe; .certs/ is gitignored.
    ...(fs.existsSync(CERT.cert) && fs.existsSync(CERT.key)
      ? { https: { cert: fs.readFileSync(CERT.cert), key: fs.readFileSync(CERT.key) } }
      : {}),

    proxy: {
      // Every path api.ts calls has to be listed here, or `npm run dev` serves
      // the SPA shell for it and the call 404s. Production never shows this --
      // Caddy fronts the API and the built assets on one origin -- so a missing
      // entry only ever costs a developer an afternoon.
      "/sighting": { target: API, changeOrigin: true },
      "/dex": { target: API, changeOrigin: true },
      "/map": { target: API, changeOrigin: true },
      "/photo": { target: API, changeOrigin: true },
      "/dogs": { target: API, changeOrigin: true },
      "/proposal": { target: API, changeOrigin: true },
      "/auth": { target: API, changeOrigin: true },
      // Server-rendered sign-in gate. Without this the SPA shell answers it and
      // the passcode form is unreachable in dev.
      "/join": { target: API, changeOrigin: true },
      "/health": { target: API, changeOrigin: true },

      // Photos, so the whole app is one HTTPS origin. Otherwise the page is
      // https and MinIO is http, and the browser blocks every image as mixed
      // content -- the map renders with empty pins and no console error worth
      // reading.
      //
      // changeOrigin MUST stay false here. Presigned URLs are SigV4, which
      // signs the Host header; rewriting Host to the proxy target invalidates
      // the signature and MinIO answers 403 on every single photo.
      [`/${BUCKET}`]: { target: S3, changeOrigin: false },
    },
  },
});
