// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  getSocialQueue,
  getSocialShared,
  passForSocial,
  pickForSocial,
  removeSocialPick,
  type SocialQueue,
  type SocialShared,
} from "../api";
import Social from "./Social";

vi.mock("../api", async (original) => ({
  ...await original<typeof import("../api")>(),
  getSocialShared: vi.fn(), getSocialQueue: vi.fn(), pickForSocial: vi.fn(),
  passForSocial: vi.fn(), removeSocialPick: vi.fn(),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const shared = (over: Partial<SocialShared> = {}): SocialShared => ({
  sighting_id: "s1", captured_at: "2026-10-24T09:00:00Z", species: "dog", handle: "a_handle",
  area: "Indiranagar", default_caption: "Spotted in Indiranagar 📸 @a_handle",
  photos: [{ photo_id: "p1", url: "p1.webp", thumb_url: "p1_t.webp" },
           { photo_id: "p2", url: "p2.webp", thumb_url: "p2_t.webp" }],
  ...over,
});
const row = (over: Partial<SocialQueue["queued"][number]> = {}) => ({
  id: "k1", sighting_id: "s9", photo_id: "p9", thumb_url: "t.webp", caption: "Spotted in A 📸 @a",
  status: "queued" as const, picked_at: "2026-10-24T08:00:00Z", posted_at: null, x_post_id: null,
  last_error: null, flagged: false, ...over,
});
const emptyQueue: SocialQueue = { queued: [], attention: [], posted: [] };

beforeEach(() => {
  vi.mocked(getSocialShared).mockResolvedValue({ items: [shared()] });
  vi.mocked(getSocialQueue).mockResolvedValue(emptyQueue);
});

describe("the POSTS screen", () => {
  it("renders shared cards with default caption", async () => {
    render(<Social onUnauthorized={() => {}} />);
    const caption = await screen.findByLabelText("Caption") as HTMLTextAreaElement;
    expect(caption.value).toBe("Spotted in Indiranagar 📸 @a_handle");
    // The full-size image, exactly what would go out, not the thumbnail.
    expect(screen.getByRole("img", { name: "photo to post" })).toHaveAttribute("src", "p1.webp");
    await userEvent.click(screen.getByRole("button", { name: "photo 2" }));
    expect(screen.getByRole("img", { name: "photo to post" })).toHaveAttribute("src", "p2.webp");
  });

  it("queue disabled while caption still has {area}", async () => {
    vi.mocked(getSocialShared).mockResolvedValue({ items: [shared({
      area: null, default_caption: "Spotted in {area} 📸 @a_handle" })] });
    render(<Social onUnauthorized={() => {}} />);
    const caption = await screen.findByLabelText("Caption");
    expect(screen.getByRole("button", { name: "Queue for X" })).toBeDisabled();
    expect(screen.getByText(/Replace \{area\}/)).toBeInTheDocument();
    await userEvent.clear(caption);
    await userEvent.type(caption, "Spotted in Hampi 📸 @a_handle");
    expect(screen.getByRole("button", { name: "Queue for X" })).toBeEnabled();
  });

  it("queues and removes the card", async () => {
    vi.mocked(pickForSocial).mockResolvedValue({ id: "new" });
    render(<Social onUnauthorized={() => {}} />);
    await screen.findByLabelText("Caption");
    await userEvent.click(screen.getByRole("button", { name: "photo 2" }));
    vi.mocked(getSocialQueue).mockResolvedValue({ ...emptyQueue, queued: [row({ id: "new", sighting_id: "s1" })] });
    await userEvent.click(screen.getByRole("button", { name: "Queue for X" }));
    expect(pickForSocial).toHaveBeenCalledWith("s1", "p2", "Spotted in Indiranagar 📸 @a_handle");
    await waitFor(() => expect(screen.queryByLabelText("Caption")).not.toBeInTheDocument());
    expect(await screen.findByText("1 QUEUED · OLDEST POSTS FIRST")).toBeInTheDocument();
  });

  it("passes on a card", async () => {
    vi.mocked(passForSocial).mockResolvedValue(undefined);
    render(<Social onUnauthorized={() => {}} />);
    await screen.findByLabelText("Caption");
    await userEvent.click(screen.getByRole("button", { name: "Not this one" }));
    expect(passForSocial).toHaveBeenCalledWith("s1");
    await waitFor(() => expect(screen.queryByLabelText("Caption")).not.toBeInTheDocument());
  });

  it("shows the server's refusal on the card", async () => {
    vi.mocked(pickForSocial).mockRejectedValue(new Error("Captions can't contain links."));
    render(<Social onUnauthorized={() => {}} />);
    await screen.findByLabelText("Caption");
    await userEvent.click(screen.getByRole("button", { name: "Queue for X" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Captions can't contain links.");
    expect(screen.getByLabelText("Caption")).toBeInTheDocument();
  });

  it("removes a queued pick", async () => {
    vi.mocked(getSocialQueue).mockResolvedValue({ ...emptyQueue, queued: [row()] });
    vi.mocked(removeSocialPick).mockResolvedValue(undefined);
    render(<Social onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(removeSocialPick).toHaveBeenCalledWith("k1");
  });

  it("shows flagged posted rows with the take-down badge", async () => {
    vi.mocked(getSocialQueue).mockResolvedValue({ ...emptyQueue, posted: [
      row({ id: "ok", status: "posted", x_post_id: "111", posted_at: "2026-10-23T03:30:00Z" }),
      row({ id: "bad", status: "posted", x_post_id: "222", posted_at: "2026-10-22T03:30:00Z", flagged: true }),
    ] });
    render(<Social onUnauthorized={() => {}} />);
    const links = await screen.findAllByRole("link", { name: /View on X/ });
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "https://x.com/nammaindies/status/111", "https://x.com/nammaindies/status/222"]);
    const badge = screen.getByText("Taken down in the app — remove this post on X");
    expect(within(badge.closest(".match-card") as HTMLElement).getByRole("link")).toHaveAttribute(
      "href", "https://x.com/nammaindies/status/222");
    expect(screen.getAllByText(/Taken down in the app/)).toHaveLength(1);
  });

  it("shows uncertain posting warning", async () => {
    vi.mocked(getSocialQueue).mockResolvedValue({ ...emptyQueue, attention: [
      row({ status: "posting", last_error: "no answer while creating the post" })] });
    render(<Social onUnauthorized={() => {}} />);
    expect(await screen.findByText(
      "Posting didn't confirm. Check @nammaindies on X before anything else posts.")).toBeInTheDocument();
  });
});
