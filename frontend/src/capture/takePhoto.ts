import { Capacitor } from "@capacitor/core";

/**
 * Native camera and camera-roll access, when running inside the Capacitor shell.
 *
 * Both use Capacitor Camera 8's `takePhoto` / `chooseFromGallery` rather than
 * `getPhoto`, which is deprecated in that major and slated for removal. The old
 * single entry point took a `source` option; the new ones are separate calls,
 * which suits us -- a camera capture and a camera-roll import are different
 * things downstream. An imported photo was taken somewhere else, some time ago,
 * so it needs its own date and place (see `readPhotoMetadata`), while a live
 * capture uses the device's own clock and GPS fix.
 *
 * Note that Camera 8's `MediaResult` carries no `format` field, unlike the old
 * `Photo`. The extension comes off the blob's own MIME type instead.
 */

/** Whether we're inside the Capacitor shell.
 *
 * Callers need this to tell "there is no native picker" from "the native picker
 * was dismissed" -- both come back as null, but only the first should fall
 * through to a web file input. Falling through on a dismissal reopens the
 * chooser as a file dialog the instant the user backs out of it.
 */
export function isNative(): boolean {
  return Capacitor.isNativePlatform();
}

function isCancellation(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return message.toLowerCase().includes("cancel");
}

async function fileFrom(
  webPath: string | undefined,
  prefix: string,
  fallbackType = "image/jpeg",
): Promise<File | null> {
  if (!webPath) return null;
  const blob = await fetch(webPath).then((r) => r.blob());
  // Camera 8's MediaResult carries no `format`, so the extension comes off the
  // blob's MIME type. The fallback has to be passed in: a video that reports no
  // type would otherwise be named .jpeg and sent as an image.
  const type = blob.type || fallbackType;
  const ext = type.split("/")[1] || fallbackType.split("/")[1];
  return new File([blob], `${prefix}-${Date.now()}.${ext}`, { type });
}

/**
 * Opens the native camera. Returns null on web (caller falls back to the
 * file-input flow) or if the user cancels the capture.
 */
export async function takePhotoIfNative(): Promise<File | null> {
  if (!Capacitor.isNativePlatform()) return null;

  const { Camera } = await import("@capacitor/camera");

  try {
    const photo = await Camera.takePhoto({ quality: 85, saveToGallery: true });
    return await fileFrom(photo.webPath, "sighting");
  } catch (err) {
    if (isCancellation(err)) return null; // expected UX, not an error to surface
    throw err; // real error -- caller should catch and toast, then fall back
  }
}

/**
 * Opens the native camera roll. Returns null on web (caller falls back to a
 * file input without `capture`, which lets the OS offer the gallery) or if the
 * user dismisses the picker.
 *
 * One photo, not a multi-select: each import carries its own capture date and
 * place, and photos chosen together are not necessarily from the same time or
 * street. Grouping several into one sighting would have to either ask per photo
 * or cluster them by EXIF -- see the follow-up issue.
 */
export async function chooseFromGalleryIfNative(): Promise<File | null> {
  if (!Capacitor.isNativePlatform()) return null;

  const { Camera } = await import("@capacitor/camera");

  try {
    const { results } = await Camera.chooseFromGallery({
      allowMultipleSelection: false,
      // No in-app editing: a crop re-encodes, and re-encoding is what strips
      // the EXIF this import depends on.
      editable: "no",
      quality: 100,
    });
    const photo = results?.[0];
    if (!photo) return null;
    return await fileFrom(photo.webPath, "import");
  } catch (err) {
    if (isCancellation(err)) return null;
    throw err;
  }
}

/**
 * Opens the native video camera. Returns null on web (caller falls back to the
 * file-input flow) or if the user cancels.
 *
 * This exists because video was the one capture path never given a native
 * route. Photos and camera-roll imports were both moved off
 * `<input type="file" capture>` onto the Camera plugin; the clip button was
 * left clicking a hidden file input, which inside a WebView depends on
 * platform file-provider behaviour rather than on anything we control.
 *
 * `saveToGallery` is false, unlike `takePhotoIfNative`. A clip is evidence the
 * server mines for frames and then discards -- `routes/sighting.py` never
 * persists the video itself -- so leaving a copy in the user's camera roll
 * would be the only lasting artefact of a thing the app deliberately throws
 * away. `isPersistent` is false for the same reason: the URI is read once,
 * immediately, and never across launches.
 */
export async function recordVideoIfNative(): Promise<File | null> {
  if (!Capacitor.isNativePlatform()) return null;

  const { Camera } = await import("@capacitor/camera");

  try {
    const clip = await Camera.recordVideo({
      saveToGallery: false,
      isPersistent: false,
    });
    return await fileFrom(clip.webPath, "clip", "video/mp4");
  } catch (err) {
    if (isCancellation(err)) return null;
    throw err;
  }
}
