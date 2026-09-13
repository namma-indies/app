// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NO_METADATA } from "../api";
import ImportOriginPrompt from "./ImportOriginPrompt";

const locate = vi.fn();
vi.mock("../capture/geolocate", () => ({
  locate: () => locate(),
  permissionState: () => Promise.resolve("prompt"),
  howToReEnable: () => "Browser settings",
}));
const mapCentre = { lat: 10.2381, lng: 77.4892 };
vi.mock("maplibre-gl", () => ({ default: { Map: class {
  on(_event: string, callback: () => void) { callback(); }
  getCenter() { return mapCentre; }
  remove() {}
} } }));

afterEach(cleanup);
beforeEach(() => locate.mockReset().mockResolvedValue({ ok: false, reason: "unavailable" }));

function setup(md = NO_METADATA) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(<ImportOriginPrompt md={md} onConfirm={onConfirm} onCancel={onCancel} />);
  fireEvent.change(screen.getByLabelText(/roughly when/), { target: { value: "2026-07-14T09:30" } });
  return { onConfirm, onCancel };
}

describe("import location picker", () => {
  it("starts empty without asking for device GPS and accepts manual coordinates after a failed fix", async () => {
    const { onConfirm } = setup();
    await userEvent.click(screen.getByText("set where it was taken"));
    expect(locate).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("USE MY LOCATION"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't get a fix");
    await userEvent.click(screen.getByText("ENTER COORDINATES"));
    expect(screen.getByLabelText("latitude")).toHaveValue("");
    expect(screen.getByLabelText("longitude")).toHaveValue("");
    await userEvent.type(screen.getByLabelText("latitude"), "10.2381");
    await userEvent.type(screen.getByLabelText("longitude"), "77.4892");
    await userEvent.click(screen.getByText("USE THESE"));
    await userEvent.click(screen.getByText("Add sighting"));
    expect(onConfirm).toHaveBeenCalledWith({
      captured_at: new Date("2026-07-14T09:30").toISOString(),
      ...mapCentre, geo_source: "pin",
    });
  });

  it("ignores a GPS result arriving after the picker was cancelled", async () => {
    let finish!: (value: unknown) => void;
    locate.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { onConfirm } = setup();
    await userEvent.click(screen.getByText("set where it was taken"));
    await userEvent.click(screen.getByText("USE MY LOCATION"));
    fireEvent.click(document.querySelector(".viewer-overlay")!);
    await act(async () => finish({ ok: true, lat: 12.9, lng: 77.6 }));
    await userEvent.click(screen.getByText("Add without a place"));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ geo_source: "none" }));
  });

  it("uses an explicitly chosen map point without device GPS", async () => {
    const { onConfirm } = setup();
    await userEvent.click(screen.getByText("set where it was taken"));
    await userEvent.click(screen.getByText("PICK ON A MAP"));
    await userEvent.click(screen.getByText("USE THIS SPOT"));
    await userEvent.click(screen.getByText("Add sighting"));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ ...mapCentre, geo_source: "pin" }));
    expect(locate).not.toHaveBeenCalled();
  });

  it("keeps file coordinates as exif after a date edit and a cancelled picker", async () => {
    const { onConfirm, onCancel } = setup({ ...NO_METADATA, lat: 12.9, lng: 77.6, has_location: true });
    expect(screen.getByText(/from the photo/)).toBeInTheDocument();
    await userEvent.click(screen.getByText("change place"));
    fireEvent.click(document.querySelector(".viewer-overlay")!);
    expect(onCancel).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("Add sighting"));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ lat: 12.9, lng: 77.6, geo_source: "exif" }));
  });

  it("preserves EXIF seconds and offset when only the location is missing", async () => {
    const onConfirm = vi.fn();
    render(<ImportOriginPrompt md={{ ...NO_METADATA, has_date: true, captured_at_local: "2026-08-05T18:42:11", utc_offset_minutes: 330 }} onConfirm={onConfirm} onCancel={vi.fn()} />);
    await userEvent.click(screen.getByText("Add without a place"));
    expect(onConfirm).toHaveBeenCalledWith({ captured_at: "2026-08-05T13:12:11.000Z", geo_source: "none" });
  });

  it("allows removing a known place without discarding the time", async () => {
    const { onConfirm } = setup({ ...NO_METADATA, lat: 12.9, lng: 77.6, has_location: true });
    await userEvent.click(screen.getByText("change place"));
    await userEvent.click(screen.getByText("save without a place"));
    await userEvent.click(screen.getByText("Add without a place"));
    expect(onConfirm).toHaveBeenCalledWith({ captured_at: new Date("2026-07-14T09:30").toISOString(), geo_source: "none" });
  });

  it.each([["91", "77", "Latitude"], ["10", "181", "Longitude"], ["", "77", "Enter both"]])("rejects invalid coordinates %s, %s", async (lat, lng, message) => {
    const { onConfirm } = setup();
    await userEvent.click(screen.getByText("set where it was taken"));
    await userEvent.click(screen.getByText("ENTER COORDINATES"));
    if (lat) await userEvent.type(screen.getByLabelText("latitude"), lat);
    await userEvent.type(screen.getByLabelText("longitude"), lng);
    await userEvent.click(screen.getByText("USE THESE"));
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
