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
}));

import { enqueue, flush } from "../offline/queue";
import { takePhotoIfNative } from "../capture/takePhoto";
import Capture from "./Capture";

afterEach(cleanup);

function makePhoto(name: string): File {
  return new File(["x"], name, { type: "image/jpeg" });
}

beforeEach(() => {
  vi.mocked(enqueue).mockReset().mockResolvedValue(undefined);
  vi.mocked(flush).mockReset().mockResolvedValue(undefined);
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
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalledTimes(1));
    const sentPhotos = vi.mocked(enqueue).mock.calls[0][0].photos as File[];
    expect(sentPhotos.map((p) => p.name)).toEqual(["one.jpg", "two.jpg"]);
  });
});
