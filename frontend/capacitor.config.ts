import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "org.nammaindies.app",
  appName: "Namma Indies",
  webDir: "dist",
  ios: {
    contentInset: "always",
  },
  plugins: {
    // WKWebView refuses to store a cross-origin Set-Cookie no matter what
    // SameSite/CORS headers say -- routing fetch()/XHR through native
    // URLSession instead sidesteps that policy entirely, with no changes
    // needed to any existing fetch() call in the app.
    CapacitorHttp: {
      enabled: true,
    },
  },
};

export default config;
