import { describe, expect, it, vi, beforeEach } from "vitest";

const isNativePlatform = vi.fn();
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => isNativePlatform() },
}));

const getPhoto = vi.fn();
vi.mock("@capacitor/camera", () => ({
  Camera: { getPhoto: (...args: unknown[]) => getPhoto(...args) },
  CameraResultType: { Uri: "uri" },
  CameraSource: { Camera: "camera" },
}));

import { takePhotoIfNative } from "./takePhoto";

beforeEach(() => {
  isNativePlatform.mockReset();
  getPhoto.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ blob: () => Promise.resolve(new Blob(["x"], { type: "image/jpeg" })) }),
  );
});

describe("takePhotoIfNative", () => {
  it("returns null on web — caller falls back to the file input", async () => {
    isNativePlatform.mockReturnValue(false);
    expect(await takePhotoIfNative()).toBeNull();
    expect(getPhoto).not.toHaveBeenCalled();
  });

  it("returns a File built from the native camera capture", async () => {
    isNativePlatform.mockReturnValue(true);
    getPhoto.mockResolvedValue({ webPath: "capacitor://blob/abc", format: "jpeg" });

    const file = await takePhotoIfNative();

    expect(file).toBeInstanceOf(File);
    expect(file?.type).toBe("image/jpeg");
    expect(getPhoto).toHaveBeenCalledWith(
      expect.objectContaining({ saveToGallery: true, source: "camera" }),
    );
  });

  it("returns null if the user cancels (no webPath)", async () => {
    isNativePlatform.mockReturnValue(true);
    getPhoto.mockRejectedValue(new Error("User cancelled photos app"));

    expect(await takePhotoIfNative()).toBeNull();
  });
});
