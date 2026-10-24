import { describe, expect, it } from "vitest";
import { buildSightingForm } from "./api";

const base = { geo_source: "none" as const, captured_at: "2026-10-24T09:00:00Z" };

describe("buildSightingForm share flag", () => {
  it("sends share_with_team only when set", () => {
    expect(buildSightingForm({ ...base, share_with_team: true }).get("share_with_team")).toBe("true");
    expect(buildSightingForm(base).get("share_with_team")).toBeNull();
  });
});
