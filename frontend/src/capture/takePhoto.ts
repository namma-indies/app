import { Capacitor } from "@capacitor/core";

/**
 * Opens the native camera when running inside the Capacitor shell.
 * Returns null on web (caller falls back to the file-input flow) or if the
 * user cancels the capture.
 */
export async function takePhotoIfNative(): Promise<File | null> {
  if (!Capacitor.isNativePlatform()) return null;

  const { Camera, CameraResultType, CameraSource } = await import("@capacitor/camera");

  let webPath: string | undefined;
  let format = "jpeg";
  try {
    const photo = await Camera.getPhoto({
      quality: 85,
      resultType: CameraResultType.Uri,
      source: CameraSource.Camera,
      saveToGallery: true,
    });
    webPath = photo.webPath;
    format = photo.format || "jpeg";
  } catch {
    return null; // user cancelled
  }
  if (!webPath) return null;

  const blob = await fetch(webPath).then((r) => r.blob());
  return new File([blob], `sighting-${Date.now()}.${format}`, {
    type: blob.type || `image/${format}`,
  });
}
