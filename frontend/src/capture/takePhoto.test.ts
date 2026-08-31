import { describe, expect, it, vi, beforeEach } from "vitest";

const isNativePlatform = vi.fn();
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => isNativePlatform() },
}));

const takePhoto = vi.fn();
const chooseFromGallery = vi.fn();
const recordVideo = vi.fn();
vi.mock("@capacitor/camera", () => ({
  Camera: {
    takePhoto: (...args: unknown[]) => takePhoto(...args),
    chooseFromGallery: (...args: unknown[]) => chooseFromGallery(...args),
    recordVideo: (...args: unknown[]) => recordVideo(...args),
  },
  CameraResultType: { Uri: "uri" },
  CameraSource: { Camera: "camera", Photos: "photos" },
}));

import {
  chooseFromGalleryIfNative,
  recordVideoIfNative,
  takePhotoIfNative,
} from "./takePhoto";

beforeEach(() => {
  isNativePlatform.mockReset();
  takePhoto.mockReset();
  chooseFromGallery.mockReset();
  recordVideo.mockReset();
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue({
        blob: () => Promise.resolve(new Blob(["x"], { type: "image/jpeg" })),
      }),
  );
});

describe("takePhotoIfNative", () => {
  it("returns null on web — caller falls back to the file input", async () => {
    isNativePlatform.mockReturnValue(false);
    expect(await takePhotoIfNative()).toBeNull();
    expect(takePhoto).not.toHaveBeenCalled();
  });

  it("returns a File built from the native camera capture", async () => {
    isNativePlatform.mockReturnValue(true);
    takePhoto.mockResolvedValue({ webPath: "capacitor://blob/abc", saved: true });

    const file = await takePhotoIfNative();

    expect(file).toBeInstanceOf(File);
    expect(file?.type).toBe("image/jpeg");
    expect(takePhoto).toHaveBeenCalledWith(
      expect.objectContaining({ saveToGallery: true }),
    );
  });

  it("uses takePhoto, not the deprecated getPhoto", async () => {
    // getPhoto is deprecated in Capacitor Camera 8 and slated for removal;
    // this asserts the migration doesn't silently regress.
    isNativePlatform.mockReturnValue(true);
    takePhoto.mockResolvedValue({ webPath: "capacitor://blob/abc", saved: true });
    await takePhotoIfNative();
    expect(takePhoto).toHaveBeenCalledTimes(1);
  });

  it("derives the extension from the blob's MIME type, since MediaResult has no format field", async () => {
    isNativePlatform.mockReturnValue(true);
    takePhoto.mockResolvedValue({ webPath: "capacitor://blob/abc", saved: true });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        blob: () => Promise.resolve(new Blob(["x"], { type: "image/png" })),
      }),
    );

    const file = await takePhotoIfNative();
    expect(file?.name).toMatch(/\.png$/);
    expect(file?.type).toBe("image/png");
  });

  it("returns null if the user cancels the camera", async () => {
    isNativePlatform.mockReturnValue(true);
    takePhoto.mockRejectedValue(new Error("User cancelled photos app"));

    expect(await takePhotoIfNative()).toBeNull();
  });

  it("throws on a non-cancel Camera error (e.g. permission denied) so the caller can surface it", async () => {
    isNativePlatform.mockReturnValue(true);
    takePhoto.mockRejectedValue(new Error("Permission denied"));

    await expect(takePhotoIfNative()).rejects.toThrow("Permission denied");
  });

  it("throws if the webPath fetch/blob conversion fails, instead of throwing out of the caller unhandled", async () => {
    isNativePlatform.mockReturnValue(true);
    takePhoto.mockResolvedValue({ webPath: "capacitor://blob/abc", saved: true });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network error")));

    await expect(takePhotoIfNative()).rejects.toThrow("network error");
  });
});

describe("chooseFromGalleryIfNative", () => {
  it("returns null on web — caller falls back to a file input without `capture`", async () => {
    isNativePlatform.mockReturnValue(false);
    expect(await chooseFromGalleryIfNative()).toBeNull();
    expect(chooseFromGallery).not.toHaveBeenCalled();
  });

  it("returns a File from the first result", async () => {
    isNativePlatform.mockReturnValue(true);
    chooseFromGallery.mockResolvedValue({
      results: [{ webPath: "capacitor://blob/old", saved: false }],
    });

    const file = await chooseFromGalleryIfNative();
    expect(file).toBeInstanceOf(File);
    expect(file?.name).toMatch(/^import-/);
  });

  it("reads `results`, the Camera 8 field, not the old `photos`", async () => {
    // pickImages returned {photos}; chooseFromGallery returns {results}. Reading
    // the wrong one yields a silent "user cancelled" on every import.
    isNativePlatform.mockReturnValue(true);
    chooseFromGallery.mockResolvedValue({
      photos: [{ webPath: "capacitor://blob/old" }],
      results: [],
    });

    expect(await chooseFromGalleryIfNative()).toBeNull();
  });

  it("asks for a single photo with no in-app editing", async () => {
    // Editing re-encodes, and re-encoding strips the EXIF the import depends on.
    isNativePlatform.mockReturnValue(true);
    chooseFromGallery.mockResolvedValue({
      results: [{ webPath: "capacitor://blob/old" }],
    });

    await chooseFromGalleryIfNative();
    expect(chooseFromGallery).toHaveBeenCalledWith(
      expect.objectContaining({ allowMultipleSelection: false, editable: "no" }),
    );
  });

  it("returns null when the picker is dismissed with nothing chosen", async () => {
    isNativePlatform.mockReturnValue(true);
    chooseFromGallery.mockResolvedValue({ results: [] });

    expect(await chooseFromGalleryIfNative()).toBeNull();
  });

  it("returns null when the user cancels", async () => {
    isNativePlatform.mockReturnValue(true);
    chooseFromGallery.mockRejectedValue(new Error("User cancelled photos app"));

    expect(await chooseFromGalleryIfNative()).toBeNull();
  });

  it("throws when the photo library permission is denied", async () => {
    isNativePlatform.mockReturnValue(true);
    chooseFromGallery.mockRejectedValue(new Error("User denied access to photos"));

    await expect(chooseFromGalleryIfNative()).rejects.toThrow("denied");
  });
});

// Video was the one capture path never given a native route: photos and
// camera-roll imports were both moved onto the Camera plugin while the clip
// button kept clicking a hidden file input, which inside a WebView depends on
// platform file-provider behaviour rather than on anything we control.
describe("recordVideoIfNative", () => {
  function blobOf(type: string) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      blob: () => Promise.resolve(new Blob(["x"], { type })),
    }));
  }

  it("returns null on web so the caller falls back to the file input", async () => {
    isNativePlatform.mockReturnValue(false);
    expect(await recordVideoIfNative()).toBeNull();
    expect(recordVideo).not.toHaveBeenCalled();
  });

  it("leaves no copy in the camera roll", async () => {
    // The server mines frames and discards the clip -- routes/sighting.py never
    // persists the video. Saving to the gallery would make the user's camera
    // roll the only lasting artefact of something the app throws away.
    isNativePlatform.mockReturnValue(true);
    recordVideo.mockResolvedValue({ webPath: "file:///clip" });
    blobOf("video/mp4");
    await recordVideoIfNative();
    expect(recordVideo).toHaveBeenCalledWith(
      expect.objectContaining({ saveToGallery: false, isPersistent: false }),
    );
  });

  it("names the file from the blob's own MIME type", async () => {
    isNativePlatform.mockReturnValue(true);
    recordVideo.mockResolvedValue({ webPath: "file:///clip" });
    blobOf("video/quicktime");
    const f = await recordVideoIfNative();
    expect(f?.type).toBe("video/quicktime");
    expect(f?.name).toMatch(/\.quicktime$/);
  });

  it("falls back to video/mp4, never the photo default", async () => {
    // Camera 8's MediaResult carries no `format` field, so the extension comes
    // off the blob. A typeless clip inheriting image/jpeg would be posted as a
    // photo, which the backend rejects.
    isNativePlatform.mockReturnValue(true);
    recordVideo.mockResolvedValue({ webPath: "file:///clip" });
    blobOf("");
    const f = await recordVideoIfNative();
    expect(f?.type).toBe("video/mp4");
    expect(f?.name).toMatch(/\.mp4$/);
  });

  it("returns null on cancellation rather than throwing", async () => {
    isNativePlatform.mockReturnValue(true);
    recordVideo.mockRejectedValue(new Error("User cancelled photos app"));
    expect(await recordVideoIfNative()).toBeNull();
  });

  it("rethrows a real failure so the caller can surface it", async () => {
    isNativePlatform.mockReturnValue(true);
    recordVideo.mockRejectedValue(new Error("camera unavailable"));
    await expect(recordVideoIfNative()).rejects.toThrow("camera unavailable");
  });

  it("returns null when the native call yields no path", async () => {
    isNativePlatform.mockReturnValue(true);
    recordVideo.mockResolvedValue({});
    expect(await recordVideoIfNative()).toBeNull();
  });
});
