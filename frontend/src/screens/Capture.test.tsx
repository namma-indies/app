// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { UPLOAD_COMPLETE_EVENT } from "../processing";
import userEvent from "@testing-library/user-event";

vi.mock("../offline/queue", () => ({
  enqueue: vi.fn(),
  flush: vi.fn(),
}));

vi.mock("../capture/takePhoto", () => ({
  takePhotoIfNative: vi.fn(),
  chooseFromGalleryIfNative: vi.fn(),
  // These tests exercise the web path, where the component drives the hidden
  // file inputs directly.
  isNative: () => false,
}));

vi.mock("../captureApi", () => ({ multiAnimalIntakeAvailable: vi.fn() }));

// The shutter path now reads the photo's own EXIF (#82). These tests drive the
// shutter with files that carry nothing, so the read must resolve to
// NO_METADATA (offline shape) rather than the real network call.
const readPhotoMetadata = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, readPhotoMetadata: (f: Blob) => readPhotoMetadata(f) };
});
import { multiAnimalIntakeAvailable } from "../captureApi";
import { enqueue, flush } from "../offline/queue";
import { takePhotoIfNative } from "../capture/takePhoto";
import Capture from "./Capture";

afterEach(cleanup);

function makePhoto(name: string): File {
  return new File(["x"], name, { type: "image/jpeg" });
}

beforeEach(() => {
  vi.mocked(multiAnimalIntakeAvailable).mockReset().mockResolvedValue(false);
  vi.mocked(enqueue).mockReset().mockResolvedValue(undefined);
  vi.mocked(flush).mockReset().mockResolvedValue(undefined);
  readPhotoMetadata.mockReset();
  // A shutter photo in these tests has no EXIF GPS, so a live capture with no
  // geolocation stubbed is a placeless save — the first LOG IT press surfaces
  // the confirm (#82) and a second press is what actually saves.
  readPhotoMetadata.mockResolvedValue({
    captured_at_local: null,
    utc_offset_minutes: null,
    lat: null,
    lng: null,
    has_date: false,
    has_location: false,
  });
  // Default to the web/no-native-camera outcome so existing tests (which
  // drive the hidden file input directly) are unaffected.
  vi.mocked(takePhotoIfNative).mockReset().mockResolvedValue(null);
  // jsdom has no createObjectURL; keep it deterministic and traceable to the
  // source file so tests can assert on which photo a thumbnail renders.
  Object.defineProperty(URL, "createObjectURL", {
    value: (b: Blob) => `blob:${(b as File).name}`,
    writable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", { value: () => {}, writable: true });
});

// A live capture with no place now requires an explicit confirm before it
// saves (#82). These tests don't care about the place, so press the confirm
// if the first LOG IT didn't save immediately.
async function logItAndSave() {
  await userEvent.click(screen.getByRole("button", { name: /LOG IT/ }));
  const confirm = screen.queryByText("LOG WITHOUT A PLACE");
  if (confirm) await userEvent.click(confirm);
  await waitFor(() => expect(enqueue).toHaveBeenCalled());
}

describe("multi-animal intake", () => {
  it.each([false, true])("persists the chosen endpoint when capability is %s", async (enabled) => {
    vi.mocked(multiAnimalIntakeAvailable).mockResolvedValue(enabled);
    render(<Capture />);
    await userEvent.upload(screen.getByLabelText("capture photo"), makePhoto("one.jpg"));
    if (enabled) {
      expect(screen.getByText(/One upload, separate animal entries/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /tell us more/ })).not.toBeInTheDocument();
    } else {
      expect(screen.getByRole("button", { name: /tell us more/ })).toBeInTheDocument();
    }
    await logItAndSave();
    await waitFor(() => expect(enqueue).toHaveBeenCalledOnce());
    expect(vi.mocked(enqueue).mock.calls[0][0].upload_endpoint).toBe(enabled ? "capture" : "sighting");
  });
  it("announces capture receipt without claiming that animal entries are already published", async () => {
    render(<Capture />);
    await act(async () => {});
    act(() => window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT, { detail: { capture_id: "c", sighting_ids: [], processing_state: "queued" } })));
    expect(screen.getByText("Upload saved · check your Journal for separate animal entries or private review")).toBeInTheDocument();
  });
});

describe("upload acknowledgement", () => {
  it("separates saving on the device from server processing", async () => {
    render(<Capture />);
    await userEvent.upload(screen.getByLabelText("capture photo"), makePhoto("one.jpg"));
    await logItAndSave();
    await waitFor(() => expect(screen.getByText("Saved on this device · waiting to upload")).toBeInTheDocument());
    act(() => window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT, { detail: { sighting_id: "q", photo_ids: [], processing_state: "queued" } })));
    expect(screen.getByText("Uploaded · waiting to process")).toBeInTheDocument();
    expect(screen.queryByText("Saved on this device · waiting to upload")).not.toBeInTheDocument();
  });
});

describe("burst photo capture", () => {
  it("adds each captured photo to a filmstrip instead of replacing the previous one", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));
    await userEvent.upload(input, makePhoto("three.jpg"));

    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(3);
  });

  it("stops accepting new photos once 5 are captured", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    for (let i = 0; i < 6; i++) {
      await userEvent.upload(input, makePhoto(`p${i}.jpg`));
    }

    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(5);
    expect(screen.getByText("5/5 photos added")).toBeInTheDocument();
    expect(container.querySelector(".filmstrip-add")).not.toBeInTheDocument();
  });

  it("removes only the tapped photo, keeping the others", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));

    await userEvent.click(screen.getByLabelText("Remove photo 1"));

    const thumbs = container.querySelectorAll<HTMLImageElement>(".filmstrip-thumb img");
    expect(thumbs).toHaveLength(1);
    expect(thumbs[0]).toHaveAttribute("src", "blob:two.jpg");
  });

  it("CLEAR ALL empties the whole set, back to the initial shutter", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));
    await userEvent.click(screen.getByText("CLEAR ALL"));

    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(0);
    expect(screen.getByText("Tap to open camera")).toBeInTheDocument();
  });

  it("clicking the shutter button falls through to the hidden file input on web", async () => {
    render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");

    await userEvent.click(screen.getByLabelText("Spot a sighting"));

    await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));

    // The fallback path still works end to end: the input can still receive
    // a file and have it flow into the filmstrip.
    await userEvent.upload(input, makePhoto("shutter.jpg"));
    expect(screen.getByAltText("captured dog")).toBeInTheDocument();
  });

  it("shows a toast and still falls back to the file input when the native camera errors (not a cancel)", async () => {
    vi.mocked(takePhotoIfNative).mockRejectedValue(new Error("Permission denied"));
    render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");

    await userEvent.click(screen.getByLabelText("Spot a sighting"));

    expect(await screen.findByText("Couldn't open camera. Try again.")).toBeInTheDocument();
    await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));
  });

  it("submits every captured photo, not just the first", async () => {
    render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));
    await logItAndSave();

    await waitFor(() => expect(enqueue).toHaveBeenCalledTimes(1));
    const sentPhotos = vi.mocked(enqueue).mock.calls[0][0].photos as File[];
    expect(sentPhotos.map((p) => p.name)).toEqual(["one.jpg", "two.jpg"]);
  });
});
