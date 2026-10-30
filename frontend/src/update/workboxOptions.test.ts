import { describe, expect, it } from "vitest";
import { workboxOptions } from "../../pwaWorkbox";

describe("service worker activation (#175)", () => {
  it("takes over immediately; nothing else would activate a waiting worker", () => {
    expect(workboxOptions.skipWaiting).toBe(true);
    expect(workboxOptions.clientsClaim).toBe(true);
    expect(workboxOptions.cleanupOutdatedCaches).toBe(true);
  });
});
