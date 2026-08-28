// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EXIF_HEAD_BYTES, NO_METADATA, readPhotoMetadata } from "./api";

function bigFile(bytes: number): File {
  return new File([new Uint8Array(bytes)], "old.jpg", { type: "image/jpeg" });
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("readPhotoMetadata", () => {
  it("uploads only the head of the file, not the whole photo", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        captured_at_local: "2026-08-05T18:42:11",
        utc_offset_minutes: 330,
        lat: 12.9,
        lng: 77.6,
        has_date: true,
        has_location: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    // A realistic 4MB phone photo. EXIF's APP1 segment is capped at 64KB by
    // the spec and sits at the front, so the head always contains it -- this
    // is what keeps the preflight from doubling the upload.
    await readPhotoMetadata(bigFile(4 * 1024 * 1024));

    const form = fetchMock.mock.calls[0][1].body as FormData;
    const head = form.get("head") as Blob;
    expect(head.size).toBe(EXIF_HEAD_BYTES);
  });

  it("sends the whole file when it is smaller than the head size", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => NO_METADATA,
    });
    vi.stubGlobal("fetch", fetchMock);

    await readPhotoMetadata(bigFile(2048));

    const form = fetchMock.mock.calls[0][1].body as FormData;
    expect((form.get("head") as Blob).size).toBe(2048);
  });

  it("returns what the server found", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          captured_at_local: "2026-08-05T18:42:11",
          utc_offset_minutes: null,
          lat: null,
          lng: null,
          has_date: true,
          has_location: false,
        }),
      }),
    );

    const md = await readPhotoMetadata(bigFile(1000));
    expect(md.has_date).toBe(true);
    expect(md.has_location).toBe(false);
  });

  it("resolves to 'nothing found' when offline, so the import still proceeds", async () => {
    // The IndexedDB queue exists precisely because the network is unreliable on
    // the street. A failed preflight must fall through to asking the person,
    // not strand them mid-import.
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    expect(await readPhotoMetadata(bigFile(1000))).toEqual(NO_METADATA);
  });

  it("resolves to 'nothing found' on a server error too", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }),
    );

    expect(await readPhotoMetadata(bigFile(1000))).toEqual(NO_METADATA);
  });

  it("still reports an expired session, which is not a 'nothing found'", async () => {
    // A 401 means sign in again; swallowing it would leave the user picking
    // photos into a void.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }),
    );

    await expect(readPhotoMetadata(bigFile(1000))).rejects.toThrow();
  });
});
