// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { deleteCapture, deleteSighting, getDex, getMap, getMe, HttpError, type Sighting } from "../api";
import { getCaptures } from "../captureApi";
import { DELETE_PHOTO_WARNING } from "../components/DeleteSighting";
import Dex from "./Dex";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(),
  getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn(), deleteCapture: vi.fn(), deleteSighting: vi.fn() }));
vi.mock("../captureApi", async (original) => ({ ...await original<typeof import("../captureApi")>(), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>map</div> }));

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.useRealTimers(); });

const sighting = (over: Partial<Sighting> = {}): Sighting => ({ id: "a", capture_id: "c1", species: "dog", captured_at: "2026-09-15T12:00:00Z",
  lat: 12, lng: 77, geo_accuracy_m: 5, attrs: {}, processing_state: "ready", photos: [{ url: "a.webp", thumb_url: "a_thumb.webp" }], ...over });

async function open(sightings: Sighting[]) {
  vi.mocked(getDex).mockResolvedValue({ sightings });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  render(<Dex onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  await userEvent.click((await screen.findAllByAltText("dog sighting"))[0]);
}

describe("Deleting from the Journal viewer (#54)", () => {
  it("deletes the whole upload after asking, and closes the viewer", async () => {
    const ask = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(deleteCapture).mockResolvedValue();
    await open([sighting()]);
    expect(screen.queryByRole("button", { name: "Remove this dog" })).not.toBeInTheDocument();   // one animal: nothing to choose
    await userEvent.click(screen.getByRole("button", { name: "Delete photo" }));
    expect(ask).toHaveBeenCalledWith(DELETE_PHOTO_WARNING);
    expect(deleteCapture).toHaveBeenCalledWith("c1");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete photo" })).not.toBeInTheDocument());
    expect(getDex).toHaveBeenCalledTimes(2);                                        // looked again
  });

  it("does nothing when you cancel", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await open([sighting()]);
    await userEvent.click(screen.getByRole("button", { name: "Delete photo" }));
    expect(deleteCapture).not.toHaveBeenCalled();
  });

  it("offers removing just this animal on a photo with several", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(deleteSighting).mockResolvedValue();
    await open([sighting(), sighting({ id: "b", captured_at: "2026-09-14T12:00:00Z" })]);
    await userEvent.click(screen.getByRole("button", { name: "Remove this dog" }));
    expect(deleteSighting).toHaveBeenCalledWith("a");
    expect(deleteCapture).not.toHaveBeenCalled();
  });

  it("says so when it fails, and keeps the viewer open", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(deleteCapture).mockRejectedValue(new HttpError(503));
    await open([sighting()]);
    await userEvent.click(screen.getByRole("button", { name: "Delete photo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t delete that just now");
    expect(screen.getByRole("button", { name: "Delete photo" })).toBeInTheDocument();
  });
});
