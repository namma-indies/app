import { webcrypto } from "node:crypto";
import { afterEach, expect, it, vi } from "vitest";
import { passcodeProof, solveProof } from "./authProof";

afterEach(() => vi.unstubAllGlobals());

it("solves the server's SHA-256 format at a non-byte-aligned difficulty", async () => {
  vi.stubGlobal("crypto", webcrypto);
  const solution = await solveProof("a-signed-server-challenge", 9, 30);
  const hash = new Uint8Array(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(`a-signed-server-challenge:${solution}`)));
  expect(hash[0]).toBe(0);
  expect(hash[1] & 128).toBe(0);
});

it("offers email when Web Crypto is unavailable", async () => {
  vi.stubGlobal("crypto", {});
  await expect(solveProof("work", 8, 30)).rejects.toThrow("sign in by email");
});

it("stops at the challenge's deadline with a way out, rather than spinning", async () => {
  // An escalated challenge is harder and its deadline shorter; a phone too
  // slow for it gets told so, and pointed at email.
  vi.stubGlobal("crypto", webcrypto);
  await expect(solveProof("hard-work", 22, 0.05)).rejects.toThrow("timed out. Please try again or sign in by email");
});

it("refuses work past what the server may ask for with a plain error", async () => {
  vi.stubGlobal("crypto", webcrypto);
  await expect(solveProof("work", 23, 30)).rejects.toThrow("The sign-in check failed. Please try again.");
});

it("says when the server has made the check harder, and solves it at that difficulty", async () => {
  vi.stubGlobal("crypto", webcrypto);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true, json: async () => ({ challenge: "escalated-work", difficulty: 12, expires_in: 120, escalated: true }),
  }));
  const onEscalated = vi.fn();
  const { challenge, solution } = await passcodeProof(onEscalated);
  expect(onEscalated).toHaveBeenCalledOnce();
  expect(challenge).toBe("escalated-work");
  const hash = new Uint8Array(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(`escalated-work:${solution}`)));
  expect(hash[0]).toBe(0);
  expect(hash[1] & 0xf0).toBe(0);
});

it("does not call it slow when it is not", async () => {
  vi.stubGlobal("crypto", webcrypto);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true, json: async () => ({ challenge: "calm", difficulty: 8, expires_in: 300, escalated: false }),
  }));
  const onEscalated = vi.fn();
  await passcodeProof(onEscalated);
  expect(onEscalated).not.toHaveBeenCalled();
});
