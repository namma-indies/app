import { Capacitor, registerPlugin, type PluginListenerHandle } from "@capacitor/core";

/**
 * Live instance segmentation of the animals in front of the camera.
 *
 * WHAT THIS IS FOR
 * ----------------
 * The server's `best_animal_box` returns exactly one box, the largest, so when
 * two dogs share a frame the second one is discarded before it can ever be
 * embedded or matched. This lets a person see, while they are still standing
 * there, that there are two animals and which is which — so the photo they
 * take is a deliberate choice rather than one the server makes for them
 * afterwards.
 *
 * WHAT CROSSES THE BRIDGE, AND WHAT DOES NOT
 * ------------------------------------------
 * The camera preview and the masks are drawn natively, underneath a
 * transparent WebView. Only boxes and counts arrive here, a few times a second
 * rather than thirty. A mask is about 6,400 floats per animal per frame; at
 * 30 fps through a JSON boundary that budget simply does not exist.
 *
 * So the React side owns the controls and the copy, and never the pixels.
 *
 * ANDROID ONLY, FOR NOW
 * ---------------------
 * `isAvailable()` is false on web and iOS. There is no web fallback on purpose:
 * a WASM segmenter in the WebView would run at a few frames a second, and a
 * viewfinder that lags behind the animal is worse than no viewfinder, because
 * it invites people to trust an outline that has moved on.
 */

export type AnimalKind = "dog" | "cat";

/** One detected animal. Box coordinates are fractions of the model's square
 * input, so they stay meaningful if the resolution changes. */
export interface DetectedAnimal {
  x: number;
  y: number;
  w: number;
  h: number;
  confidence: number;
  kind: AnimalKind;
}

export interface SegmenterState {
  running: boolean;
  /** False means inference fell back to the CPU. Surface it: minSdk is 24, so
   * some devices will land here, and an app quietly running at 6 fps looks
   * broken with no way for anyone to tell why. */
  gpu: boolean;
  /** Model input edge in pixels: 320 by default, 256 on slower hardware. */
  size: number;
  fps: number;
}

export interface AnimalsEvent extends SegmenterState {
  animals: DetectedAnimal[];
}

export interface StartOptions {
  /** 320 by default. 256 for devices that cannot hold frame rate; 640 costs
   * four times as much for no additional animals found on our own fixtures. */
  size?: 256 | 320 | 640;
}

interface DogSegmenterPlugin {
  isSupported(): Promise<{ available: boolean; gpu: boolean; running: boolean }>;
  start(options?: StartOptions): Promise<SegmenterState>;
  stop(): Promise<void>;
  addListener(
    event: "animals",
    handler: (event: AnimalsEvent) => void,
  ): Promise<PluginListenerHandle>;
}

const plugin = registerPlugin<DogSegmenterPlugin>("DogSegmenter");

/** Whether a live segmenting viewfinder exists at all on this device. Check it
 * before offering the affordance: an button that always fails is worse than an
 * absent one. */
export function isAvailable(): boolean {
  return Capacitor.getPlatform() === "android";
}

export async function isSupported() {
  if (!isAvailable()) return { available: false, gpu: false, running: false };
  try {
    return await plugin.isSupported();
  } catch {
    return { available: false, gpu: false, running: false };
  }
}

/** Start the preview. Rejects if the camera permission is refused, or if the
 * model is missing from the APK — see frontend/android/app/src/main/assets. */
export async function start(options: StartOptions = {}): Promise<SegmenterState> {
  return plugin.start(options);
}

export async function stop(): Promise<void> {
  if (!isAvailable()) return;
  try {
    await plugin.stop();
  } catch {
    // Stopping something that is not running is not an error worth surfacing,
    // and this is the call a cleanup path makes on the way out.
  }
}

export function onAnimals(
  handler: (event: AnimalsEvent) => void,
): Promise<PluginListenerHandle> {
  return plugin.addListener("animals", handler);
}
