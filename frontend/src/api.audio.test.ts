import { describe, expect, it } from "vitest";
import { buildSightingForm } from "./api";

const base = { geo_source: "none" as const, captured_at: "2026-10-24T10:00:00Z" };

describe("buildSightingForm voice note", () => {
  it("sends the note beside the photos, named for its format", () => {
    const form = buildSightingForm({
      ...base,
      photos: [new Blob(["p"], { type: "image/jpeg" })],
      audio: new Blob(["n"], { type: "audio/webm;codecs=opus" }),
    });
    expect((form.get("audio") as File).name).toBe("note.webm");
    expect(form.getAll("photos")).toHaveLength(1);
  });

  it("never sends a note with a clip", () => {
    const form = buildSightingForm({
      ...base,
      video: new Blob(["v"], { type: "video/mp4" }),
      audio: new Blob(["n"], { type: "audio/mp4" }),
    });
    expect(form.get("audio")).toBeNull();
  });

  it("sends nothing extra without a note", () => {
    expect(buildSightingForm({ ...base, photos: [] }).get("audio")).toBeNull();
  });
});
