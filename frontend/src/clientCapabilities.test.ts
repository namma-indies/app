import { describe, expect, it, vi } from "vitest";
import { CONSENT_CAPABILITY_HEADER, isOwnApi, withConsentCapability } from "./clientCapabilities";

vi.mock("./apiBase", () => ({ API_BASE: "" }));

const sent = (mock: ReturnType<typeof vi.fn<typeof fetch>>) => new Headers(mock.mock.calls[0][1]?.headers);

describe("consent capability header (#228)", () => {
  it("marks requests to our own API", async () => {
    const inner = vi.fn<typeof fetch>(async () => new Response("{}"));
    await withConsentCapability(inner)("/dex", { credentials: "include", headers: { Accept: "application/json" } });
    expect(sent(inner).get(CONSENT_CAPABILITY_HEADER)).toBe("1");
    expect(sent(inner).get("Accept")).toBe("application/json");
    expect(inner.mock.calls[0][1]?.credentials).toBe("include");
  });

  it("leaves other origins alone, such as presigned storage uploads", async () => {
    const inner = vi.fn<typeof fetch>(async () => new Response("{}"));
    await withConsentCapability(inner)("https://storage.example.invalid/bucket/key?X-Amz-Signature=x", { method: "PUT" });
    expect(sent(inner).has(CONSENT_CAPABILITY_HEADER)).toBe(false);
  });

  it("recognises relative and same-origin URLs only", () => {
    expect(isOwnApi("/account/consent", "https://app.example.invalid")).toBe(true);
    expect(isOwnApi("https://app.example.invalid/dex", "https://app.example.invalid")).toBe(true);
    expect(isOwnApi("//other.invalid/dex", "https://app.example.invalid")).toBe(false);
    expect(isOwnApi("https://app.example.invalid.other/dex", "https://app.example.invalid")).toBe(false);
  });
});
