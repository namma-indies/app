// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../offline/queue", () => ({
  enqueue: vi.fn(),
  flush: vi.fn(),
}));

vi.mock("../capture/takePhoto", () => ({
  takePhotoIfNative: vi.fn(),
  chooseFromGalleryIfNative: vi.fn(),
  isNative: () => false, // web path: the component drives the hidden input
}));

const readPhotoMetadata = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, readPhotoMetadata: (f: Blob) => readPhotoMetadata(f) };
});

import { enqueue } from "../offline/queue";
import Capture from "./Capture";

afterEach(cleanup);

const FULL_EXIF = {
  captured_at_local: "2026-08-05T18:42:11",
  utc_offset_minutes: 330,
  lat: 12.9352,
  lng: 77.6245,
  has_date: true,
  has_location: true,
};

const NOTHING = {
  captured_at_local: null,
  utc_offset_minutes: null,
  lat: null,
  lng: null,
  has_date: false,
  has_location: false,
};

function oldPhoto(name = "old.jpg"): File {
  return new File(["x"], name, { type: "image/jpeg" });
}

beforeEach(() => {
  vi.mocked(enqueue).mockReset().mockResolvedValue(undefined);
  readPhotoMetadata.mockReset();
  Object.defineProperty(URL, "createObjectURL", {
    value: (b: Blob) => `blob:${(b as File).name}`,
    writable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", { value: () => {}, writable: true });
  // No geolocation unless a test provides it -- imports must never silently
  // fall back to the current position.
  Object.defineProperty(navigator, "geolocation", {
    value: undefined,
    writable: true,
    configurable: true,
  });
  // Belt and braces: `value: undefined` leaves the key in place, so anything
  // testing `"geolocation" in navigator` would still see it.
});

async function importFile(file = oldPhoto()) {
  const input = screen.getByLabelText("choose from photos") as HTMLInputElement;
  await userEvent.upload(input, file);
}

describe("camera-roll import: the photo's own date and place", () => {
  it("logs an EXIF-complete photo as then-and-there, without asking anything", async () => {
    readPhotoMetadata.mockResolvedValue(FULL_EXIF);
    render(<Capture />);

    await importFile();
    await waitFor(() => expect(screen.getByText(/^from your photos ·/)).toBeInTheDocument());
    // No prompt: the file answered both questions.
    expect(screen.queryByText("ABOUT THIS PHOTO")).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalled());
    const input = vi.mocked(enqueue).mock.calls[0][0];
    // 18:42:11 +05:30 == 13:12:11Z. Not "now".
    expect(input.captured_at).toBe("2026-08-05T13:12:11.000Z");
    expect(input.lat).toBeCloseTo(12.9352, 5);
    expect(input.lng).toBeCloseTo(77.6245, 5);
    expect(input.geo_source).toBe("exif");
  });

  it("never records an import as here-and-now", async () => {
    // The failure this whole feature exists to prevent: captured_at and geog
    // are the two inputs to resolve_sighting's 1km candidate search, so an
    // import stamped with the current time and place inserts a phantom into
    // the spatial prior for wherever the phone is standing.
    readPhotoMetadata.mockResolvedValue(FULL_EXIF);
    render(<Capture />);

    await importFile();
    await waitFor(() => expect(screen.getByText(/^from your photos ·/)).toBeInTheDocument());
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalled());
    const { captured_at } = vi.mocked(enqueue).mock.calls[0][0];
    const secondsAgo = (Date.now() - +new Date(captured_at)) / 1000;
    expect(secondsAgo).toBeGreaterThan(60);
  });

  it("asks when the file carries nothing", async () => {
    readPhotoMetadata.mockResolvedValue(NOTHING);
    render(<Capture />);

    await importFile();

    await waitFor(() =>
      expect(screen.getByText("ABOUT THIS PHOTO")).toBeInTheDocument(),
    );
    expect(screen.getByLabelText(/roughly when/)).toBeInTheDocument();
  });

  it("asks only for the missing half when the date survived but GPS didn't", async () => {
    readPhotoMetadata.mockResolvedValue({ ...NOTHING, ...{ captured_at_local: "2026-08-05T18:42:11", has_date: true } });
    render(<Capture />);

    await importFile();

    await waitFor(() =>
      expect(screen.getByText("ABOUT THIS PHOTO")).toBeInTheDocument(),
    );
    // Prefilled from the file rather than blank.
    expect((screen.getByLabelText(/roughly when/) as HTMLInputElement).value).toBe(
      "2026-08-05T18:42",
    );
    expect(screen.getByText("use my current location")).toBeInTheDocument();
  });

  it("uses the time the person typed, in their own zone", async () => {
    readPhotoMetadata.mockResolvedValue(NOTHING);
    render(<Capture />);
    await importFile();
    await waitFor(() => screen.getByText("ABOUT THIS PHOTO"));

    const whenInput = screen.getByLabelText(/roughly when/) as HTMLInputElement;
    await userEvent.clear(whenInput);
    await userEvent.type(whenInput, "2026-07-14T09:30");
    await userEvent.click(screen.getByText("Add without a place"));

    await waitFor(() => expect(screen.getByText(/^from your photos ·/)).toBeInTheDocument());
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalled());
    const input = vi.mocked(enqueue).mock.calls[0][0];
    // A typed value has no stated offset, so it means local time.
    expect(input.captured_at).toBe(new Date(2026, 6, 14, 9, 30, 0).toISOString());
  });

  it("saves with no coordinate when the place is skipped, rather than refusing the photo", async () => {
    // A sighting with no coordinate still counts -- it just draws no pin,
    // exactly like an offline capture. Refusing it would throw away a real
    // photo of a real dog over a field nobody remembers.
    readPhotoMetadata.mockResolvedValue(NOTHING);
    render(<Capture />);
    await importFile();
    await waitFor(() => screen.getByText("ABOUT THIS PHOTO"));

    const whenInput = screen.getByLabelText(/roughly when/) as HTMLInputElement;
    await userEvent.clear(whenInput);
    await userEvent.type(whenInput, "2026-07-14T09:30");
    await userEvent.click(screen.getByText("Add without a place"));
    await waitFor(() => screen.getByText(/^from your photos ·/));
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalled());
    const input = vi.mocked(enqueue).mock.calls[0][0];
    expect(input.geo_source).toBe("none");
    expect(input.lat).toBeUndefined();
    expect(input.lng).toBeUndefined();
  });

  it("marks a person-supplied location as a pin, not as exif", async () => {
    readPhotoMetadata.mockResolvedValue(NOTHING);
    Object.defineProperty(navigator, "geolocation", {
      value: {
        getCurrentPosition: (ok: PositionCallback) =>
          ok({ coords: { latitude: 12.9, longitude: 77.6, accuracy: 12 } } as GeolocationPosition),
      },
      writable: true,
      configurable: true,
    });
    render(<Capture />);
    await importFile();
    await waitFor(() => screen.getByText("ABOUT THIS PHOTO"));

    const whenInput = screen.getByLabelText(/roughly when/) as HTMLInputElement;
    await userEvent.clear(whenInput);
    await userEvent.type(whenInput, "2026-07-14T09:30");
    await userEvent.click(screen.getByText("use my current location"));
    await waitFor(() => expect(screen.getByText(/12\.9000, 77\.6000/)).toBeInTheDocument());
    await userEvent.click(screen.getByText("Add sighting"));
    await waitFor(() => screen.getByText(/^from your photos ·/));
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalled());
    const input = vi.mocked(enqueue).mock.calls[0][0];
    expect(input.geo_source).toBe("pin");
    expect(input.lat).toBeCloseTo(12.9, 4);
  });

  it("refuses to save with no date at all", async () => {
    readPhotoMetadata.mockResolvedValue(NOTHING);
    render(<Capture />);
    await importFile();
    await waitFor(() => screen.getByText("ABOUT THIS PHOTO"));

    await userEvent.click(screen.getByText("Add without a place"));

    expect(screen.getByText(/rough date and time/)).toBeInTheDocument();
    expect(enqueue).not.toHaveBeenCalled();
  });

  it("still asks when the preflight fails offline, instead of stranding the import", async () => {
    // readPhotoMetadata resolves to NO_METADATA on a network failure by design,
    // so an offline import lands in the prompt like any stripped photo.
    readPhotoMetadata.mockResolvedValue(NOTHING);
    render(<Capture />);

    await importFile();

    await waitFor(() =>
      expect(screen.getByText("ABOUT THIS PHOTO")).toBeInTheDocument(),
    );
  });

  it("cancelling the prompt stages nothing", async () => {
    readPhotoMetadata.mockResolvedValue(NOTHING);
    const { container } = render(<Capture />);
    await importFile();
    await waitFor(() => screen.getByText("ABOUT THIS PHOTO"));

    await userEvent.click(screen.getByText("Cancel"));

    expect(screen.queryByText("ABOUT THIS PHOTO")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(0);
    expect(screen.queryByText(/^from your photos ·/)).not.toBeInTheDocument();
  });

  it("does not offer to add more photos beside an import", async () => {
    // One import is one sighting: a live camera photo added next to it would be
    // from a different time and street, under one date and place.
    readPhotoMetadata.mockResolvedValue(FULL_EXIF);
    const { container } = render(<Capture />);

    await importFile();
    await waitFor(() => screen.getByText(/^from your photos ·/));

    expect(container.querySelector(".filmstrip-add")).not.toBeInTheDocument();
  });

  it("only sends the head of the file to the preflight", async () => {
    readPhotoMetadata.mockResolvedValue(FULL_EXIF);
    render(<Capture />);

    await importFile(oldPhoto("big.jpg"));

    await waitFor(() => expect(readPhotoMetadata).toHaveBeenCalled());
    // The slicing itself lives in readPhotoMetadata (covered in
    // photoMetadata.test.ts); this asserts the component hands it the file
    // rather than reading bytes itself.
    expect(readPhotoMetadata.mock.calls[0][0]).toBeInstanceOf(File);
  });

  it("clearing everything drops the imported date and place", async () => {
    readPhotoMetadata.mockResolvedValue(FULL_EXIF);
    render(<Capture />);
    await importFile();
    await waitFor(() => screen.getByText(/^from your photos ·/));

    await userEvent.click(screen.getByText("CLEAR ALL"));

    // Back to the empty state, so a subsequent live capture is not logged with
    // the import's timestamp.
    expect(screen.queryByText(/^from your photos ·/)).not.toBeInTheDocument();
    expect(screen.getByText("SPOT AN INDIE")).toBeInTheDocument();
  });

  it("a live camera capture is unaffected — still here and now", async () => {
    render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;
    await userEvent.upload(input, oldPhoto("live.jpg"));
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalled());
    const sent = vi.mocked(enqueue).mock.calls[0][0];
    expect(sent.geo_source).toBe("none"); // no geolocation stubbed in this test
    expect(Date.now() - +new Date(sent.captured_at)).toBeLessThan(60_000);
    expect(readPhotoMetadata).not.toHaveBeenCalled();
  });
});
