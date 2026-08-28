import { describe, expect, it, vi, beforeEach } from "vitest";

const isNativePlatform = vi.fn();
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => isNativePlatform() },
}));

const takePhoto = vi.fn();
const chooseFromGallery = vi.fn();
vi.mock("@capacitor/camera", () => ({
  Camera: {
    takePhoto: (...args: unknown[]) => takePhoto(...args),
    chooseFromGallery: (...args: unknown[]) => chooseFromGallery(...args),
  },
  CameraResultType: { Uri: "uri" },
  CameraSource: { Camera: "camera", Photos: "photos" },
}));

import { chooseFromGalleryIfNative, takePhotoIfNative } from "./takePhoto";

beforeEach(() => {
  isNativePlatform.mockReset();
  takePhoto.mockReset();
  chooseFromGallery.mockReset();
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
