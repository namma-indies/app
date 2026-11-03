import { API_BASE } from "./apiBase";

export async function solveProof(challenge: string, difficulty: number, expiresIn: number): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("This browser cannot run the sign-in check. Please sign in by email.");
  if (!Number.isInteger(difficulty) || difficulty < 8 || difficulty > 22 || expiresIn <= 0 || expiresIn > 600) {
    throw new Error("The sign-in check failed. Please try again.");
  }
  const encoder = new TextEncoder();
  const deadline = Date.now() + expiresIn * 1000;
  // Bounded batches leave the UI responsive and avoid one native bridge call
  // per digest. Work is local; no script or visitor data goes to a vendor.
  for (let n = 0; n < 2 ** 32 && Date.now() < deadline; n += 64) {
    const hashes = await Promise.all(Array.from({ length: 64 }, (_, i) =>
      crypto.subtle.digest("SHA-256", encoder.encode(`${challenge}:${n + i}`))));
    const found = hashes.findIndex((hash) => {
      const bytes = new Uint8Array(hash);
      for (let bit = 0; bit < difficulty; bit++) {
        if (bytes[bit >> 3] & (128 >> (bit & 7))) return false;
      }
      return true;
    });
    if (found >= 0) return String(n + found);
  }
  throw new Error("The sign-in check timed out. Please try again or sign in by email.");
}

export async function passcodeProof(): Promise<{ challenge: string; solution: string }> {
  const res = await fetch(`${API_BASE}/auth/join/challenge`, { credentials: "include", headers: { Accept: "application/json" } });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? data.detail ?? "The sign-in check is unavailable. Please try again.");
  const solution = await solveProof(data.challenge, data.difficulty, data.expires_in);
  return { challenge: data.challenge, solution };
}
