import { webcrypto } from "node:crypto";
import { afterEach, expect, it, vi } from "vitest";
import { solveProof } from "./authProof";

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
