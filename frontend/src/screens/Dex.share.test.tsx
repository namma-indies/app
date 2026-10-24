// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDex, getMap, getMe, setSightingShared } from "../api";
import { getCaptures } from "../captureApi";
import Dex from "./Dex";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn(), setSightingShared: vi.fn() }));
vi.mock("../captureApi", async (original) => ({ ...await original<typeof import("../captureApi")>(), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>map</div> }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const mine = (id: string, shared: boolean) => ({ id, captured_at: "2026-10-24T09:00:00Z", lat: 12, lng: 77, geo_accuracy_m: 5, attrs: {}, photos: [{ url: `${id}.webp`, thumb_url: `${id}_t.webp` }], shared_with_team: shared });

async function openJournal(canShare: boolean) {
  vi.mocked(getDex).mockResolvedValue({ sightings: [mine("a", false), mine("b", true)] });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false, can_share_socially: canShare } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  render(<Dex onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  await screen.findByText("YOUR GUIDE · 2 SIGHTINGS");
}

describe("sharing from the Journal", () => {
  it("journal toggle calls setSightingShared and reflects the result", async () => {
    vi.mocked(setSightingShared).mockResolvedValue({ shared: true });
    await openJournal(true);
    expect(await screen.findByRole("button", { name: "Shared with the team" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Share with the team" }));
    expect(setSightingShared).toHaveBeenCalledWith("a", true);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Shared with the team" })).toHaveLength(2));
  });

  it("offers nothing without consent", async () => {
    await openJournal(false);
    expect(screen.queryByRole("button", { name: /with the team/ })).not.toBeInTheDocument();
  });
});

describe("INDIEDEX's tabs", () => {
  it.each([true, false])("are MAP, JOURNAL and DOGS for everyone (moderator=%s)", async (moderator) => {
    vi.mocked(getDex).mockResolvedValue({ sightings: [] });
    vi.mocked(getMe).mockResolvedValue({ is_moderator: moderator } as Awaited<ReturnType<typeof getMe>>);
    vi.mocked(getMap).mockResolvedValue({ sightings: [] });
    vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
    render(<Dex onUnauthorized={() => {}} />);
    await screen.findByRole("button", { name: "JOURNAL" });
    await waitFor(() => expect(getMe).toHaveBeenCalled());
    // FLAGS and POSTS live under ADMIN now.
    expect(screen.queryByRole("button", { name: "FLAGS" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "POSTS" })).not.toBeInTheDocument();
  });
});
