import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { recordAcknowledged, STREAK } from "./streak";

class MemoryStorage {
  private store = new Map<string, string>();
  getItem(k: string) { return this.store.has(k) ? this.store.get(k)! : null; }
  setItem(k: string, v: string) { this.store.set(k, String(v)); }
  removeItem(k: string) { this.store.delete(k); }
  clear() { this.store.clear(); }
}

const NOW = Date.parse("2026-10-24T08:00:00Z");
const minutesBefore = (m: number) => new Date(NOW - m * 60_000).toISOString();

beforeEach(() => vi.stubGlobal("localStorage", new MemoryStorage()));
afterEach(() => vi.unstubAllGlobals());

describe("a hot streak", () => {
  it(`completes on the ${STREAK.count}th sighting within ${STREAK.windowMinutes} minutes`, () => {
    for (const m of [12, 9, 6, 3]) expect(recordAcknowledged(minutesBefore(m), NOW)).toBeNull();
    const streak = recordAcknowledged(minutesBefore(0), NOW);
    expect(streak?.count).toBe(5);
    expect(streak?.firstAt.toISOString()).toBe(minutesBefore(12));
    expect(streak?.lastAt.toISOString()).toBe(minutesBefore(0));
  });

  it("is counted over when the sightings were captured, so a backlog flush is not a streak", () => {
    // A week offline, uploaded in one second when signal returns.
    for (const m of [6 * 1440, 5 * 1440, 4 * 1440, 3 * 1440, 2 * 1440, 1440, 0]) {
      expect(recordAcknowledged(minutesBefore(m), NOW)).toBeNull();
    }
  });

  it("is not counted when spread over a slow walk", () => {
    for (const m of [80, 60, 40, 20, 0]) expect(recordAcknowledged(minutesBefore(m), NOW)).toBeNull();
  });

  it("gives an old walk uploaded late no card", () => {
    for (const m of [24, 21, 18, 15, 13].map((x) => x + 13 * 60)) {
      expect(recordAcknowledged(minutesBefore(m), NOW)).toBeNull();
    }
  });

  it("shows one card per run, and another after a real gap", () => {
    for (const m of [12, 9, 6, 3]) recordAcknowledged(minutesBefore(m), NOW);
    expect(recordAcknowledged(minutesBefore(2), NOW)).not.toBeNull();
    expect(recordAcknowledged(minutesBefore(1), NOW)).toBeNull();
    const later = NOW + 90 * 60_000;
    const at = (m: number) => new Date(later - m * 60_000).toISOString();
    for (const m of [10, 8, 6, 4]) expect(recordAcknowledged(at(m), later)).toBeNull();
    expect(recordAcknowledged(at(0), later)?.count).toBe(5);
  });

  it("ignores an acknowledgement with no usable capture time", () => {
    expect(recordAcknowledged(undefined, NOW)).toBeNull();
    expect(recordAcknowledged("yesterday-ish", NOW)).toBeNull();
  });

  it("never throws when storage is unavailable", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    });
    expect(() => recordAcknowledged(minutesBefore(0), NOW)).not.toThrow();
  });
});
