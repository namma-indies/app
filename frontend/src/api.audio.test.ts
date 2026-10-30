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

describe("the language a note is spoken in (#167)", () => {
  it("goes with the note when one was picked", () => {
    const form = buildSightingForm({ ...base, photos: [new Blob(["p"])], audio: new Blob(["n"], { type: "audio/mp4" }), note_language: "kn" });
    expect(form.get("note_language")).toBe("kn");
  });
  it("is left out when none was picked, or there is no note to go with", () => {
    expect(buildSightingForm({ ...base, photos: [], audio: new Blob(["n"], { type: "audio/mp4" }) }).get("note_language")).toBeNull();
    expect(buildSightingForm({ ...base, photos: [], note_language: "kn" }).get("note_language")).toBeNull();
    expect(buildSightingForm({ ...base, video: new Blob(["v"]), audio: new Blob(["n"]), note_language: "kn" }).get("note_language")).toBeNull();
  });
});

describe("the usual language, sent with a note in case its own save failed (#168 review)", () => {
  it("goes with the note, beside any choice for it", () => {
    const form = buildSightingForm({ ...base, photos: [], audio: new Blob(["n"], { type: "audio/mp4" }), usual_language: "kn" });
    expect(form.get("usual_language")).toBe("kn");
    // A clip's sound is transcribed with it as the hint too (#168 review).
    expect(buildSightingForm({ ...base, video: new Blob(["v"], { type: "video/mp4" }), usual_language: "kn" }).get("usual_language")).toBe("kn");
  });
});
