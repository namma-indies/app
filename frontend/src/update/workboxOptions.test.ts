import { describe, expect, it } from "vitest";
import { workboxOptions } from "../../pwaWorkbox";
import { SERVER_ROUTES } from "../serverRoutes";

describe("service worker activation (#175)", () => {
  it("takes over immediately; nothing else would activate a waiting worker", () => {
    expect(workboxOptions.skipWaiting).toBe(true);
    expect(workboxOptions.clientsClaim).toBe(true);
    expect(workboxOptions.cleanupOutdatedCaches).toBe(true);
  });
});

describe("service worker navigation", () => {
  it("uses the shared server route denylist", () => {
    expect(workboxOptions.navigateFallbackDenylist).toBe(SERVER_ROUTES);
  });

  it.each([
    "/account/delete",
    "/account/delete?source=web",
    "/account/delete/confirm?token=abc",
    "/auth/",
    "/auth/email/consume?token=abc",
    "/join",
  ])("leaves %s to the server", (path) => {
    expect(workboxOptions.navigateFallbackDenylist?.some((route) => route.test(path))).toBe(true);
  });

  it.each(["/", "/dex"])("keeps %s in the app shell", (path) => {
    expect(workboxOptions.navigateFallbackDenylist?.some((route) => route.test(path))).toBe(false);
  });
});
