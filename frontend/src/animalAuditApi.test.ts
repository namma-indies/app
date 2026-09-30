import { afterEach, describe, expect, it, vi } from "vitest";
import { getAnimalAudit, getAnimalAuditDetail, HttpError, ruleOnAnimal, ruleOnCaptureAnimal, UnauthorizedError } from "./api";

const ok = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
afterEach(() => vi.unstubAllGlobals());

describe("animal audit API", () => {
  it("encodes the opaque cursor and passes pagination, credentials and cancellation", async () => {
    const page = { items: [], next_cursor: "next" };
    const fetch = vi.fn().mockResolvedValue(ok(page));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    expect(await getAnimalAudit("needs_review", "date+id/with=padding", controller.signal)).toEqual(page);
    const [url, init] = fetch.mock.calls[0];
    const query = new URL(url, "https://example.test").searchParams;
    expect(query.get("filter")).toBe("needs_review");
    expect(query.get("before")).toBe("date+id/with=padding");
    expect(query.get("limit")).toBe("20");
    expect(init).toEqual({ credentials: "include", signal: controller.signal });
  });

  it("omits an initial cursor and requests detail by kind and escaped ID", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(ok({ items: [], next_cursor: null })));
    vi.stubGlobal("fetch", fetch);
    await getAnimalAudit("all");
    expect(fetch.mock.calls[0][0]).not.toContain("before=");
    const controller = new AbortController();
    await getAnimalAuditDetail("capture", "a/b", controller.signal);
    expect(fetch.mock.calls[1]).toEqual([expect.stringMatching(/\/moderation\/animal-audit\/capture\/a%2Fb$/),
      { credentials: "include", signal: controller.signal }]);
  });

  it.each(["animal", "no_animal"] as const)("sends %s as form data using the existing auth convention", async (verdict) => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(ok({ ok: true })));
    vi.stubGlobal("fetch", fetch);
    await ruleOnCaptureAnimal("c1", verdict);
    await ruleOnAnimal("s1", verdict);
    expect(fetch.mock.calls[0][0]).toMatch(/\/moderation\/animal-audit\/capture\/c1$/);
    expect(fetch.mock.calls[1][0]).toMatch(/\/sighting\/s1\/animal$/);
    for (const [, init] of fetch.mock.calls) {
      expect(init.method).toBe("POST");
      expect(init.credentials).toBe("include");
      expect(init.body).toBeInstanceOf(FormData);
      expect([...init.body.entries()]).toEqual([["verdict", verdict]]);
    }
  });

  it("preserves 401 and 409 errors rather than treating them as successful reviews", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 409 }))
      .mockResolvedValueOnce(new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetch);
    await expect(getAnimalAudit("all")).rejects.toBeInstanceOf(UnauthorizedError);
    await expect(ruleOnCaptureAnimal("c1", "animal")).rejects.toMatchObject({ status: 409 });
    await expect(getAnimalAuditDetail("sighting", "missing")).rejects.toBeInstanceOf(HttpError);
  });
});
