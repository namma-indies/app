import { describe, expect, it } from "vitest";
import { formatDay, formatDayRange, formatWhen } from "./formatWhen";

const NOW = new Date(2026, 9, 28, 12, 0, 0);

describe("formatWhen", () => {
  it("writes day and time Journal-style, in en-IN, without seconds", () => {
    expect(formatWhen(new Date(2026, 9, 21, 15, 45, 7), NOW)).toBe("21 Oct · 3:45 pm");
  });

  it("keeps the minutes at midnight and noon", () => {
    expect(formatWhen(new Date(2026, 8, 1, 0, 36, 7), NOW)).toBe("1 Sept · 12:36 am");
  });

  it("adds the year only when it is not the current one", () => {
    expect(formatWhen(new Date(2025, 1, 8, 15, 16), NOW)).toBe("8 Feb 2025 · 3:16 pm");
  });

  it("accepts an ISO string", () => {
    expect(formatWhen(new Date(2026, 9, 21, 15, 45).toISOString(), NOW)).toBe("21 Oct · 3:45 pm");
  });
});

describe("formatDay", () => {
  it("omits the current year, keeps any other", () => {
    expect(formatDay(new Date(2026, 9, 23), NOW)).toBe("23 Oct");
    expect(formatDay(new Date(2024, 9, 23), NOW)).toBe("23 Oct 2024");
  });
});

describe("formatDayRange", () => {
  it("says a same-day range once", () => {
    expect(formatDayRange("2026-10-23", "2026-10-23", NOW)).toBe("23 Oct");
  });

  it("shows both ends when they differ", () => {
    expect(formatDayRange("2026-10-21", "2026-10-23", NOW)).toBe("21 Oct → 23 Oct");
  });
});
