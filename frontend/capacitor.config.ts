import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "org.nammaindies.app",
  appName: "Namma Indies",
  webDir: "dist",
  ios: {
    contentInset: "always",
  },
};

export default config;
