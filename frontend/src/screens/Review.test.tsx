// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getProposals: vi.fn(),
  resolveProposal: vi.fn(),
}));

import {
  getProposals,
  HttpError,
  resolveProposal,
  UnauthorizedError,
  type Proposal,
  type ProposalsResponse,
} from "../api";
import Review from "./Review";

afterEach(cleanup);

function proposal(over: Partial<Proposal> = {}): Proposal {
  return {
    id: "p1",
    score: 0.62,
    a: { sighting_id: "s1", date: "2026-07-01", thumb_url: "https://x.test/a_thumb.webp" },
    b: { sighting_id: "s2", date: "2026-07-09", thumb_url: "https://x.test/b_thumb.webp" },
    ...over,
  };
}

function resp(proposals: Proposal[]): ProposalsResponse {
  return { proposals, propose_min: 0.4 };
}

describe("Review", () => {
  it("shows both photos, because that is the whole question", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal()]));
    render(<Review onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getAllByAltText("sighting")).toHaveLength(2));
    expect(screen.getByText(/SIMILARITY 0\.62/)).toBeInTheDocument();
  });

  it("confirms before merging — a merge has no undo in the app", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal()]));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Review onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("SAME ANIMAL")).toBeInTheDocument());
    await userEvent.click(screen.getByText("SAME ANIMAL"));
    expect(confirmSpy).toHaveBeenCalled();
    expect(resolveProposal).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("records a merge once confirmed and drops it from the queue", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal()]));
    vi.mocked(resolveProposal).mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Review onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("SAME ANIMAL")).toBeInTheDocument());
    await userEvent.click(screen.getByText("SAME ANIMAL"));
    await waitFor(() => expect(resolveProposal).toHaveBeenCalledWith("p1", "same"));
    await waitFor(() => expect(screen.getByText(/NOTHING TO REVIEW/)).toBeInTheDocument());
    confirmSpy.mockRestore();
  });

  it("does not confirm for 'different' — that verdict is cheap to correct", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal()]));
    vi.mocked(resolveProposal).mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Review onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("DIFFERENT ANIMALS")).toBeInTheDocument());
    await userEvent.click(screen.getByText("DIFFERENT ANIMALS"));
    await waitFor(() => expect(resolveProposal).toHaveBeenCalledWith("p1", "different"));
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("explains an empty queue rather than looking broken", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([]));
    render(<Review onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/MATCHES APPEAR WHEN TWO SIGHTINGS LOOK ALIKE/))
        .toBeInTheDocument());
  });

  it("marks which photo is yours", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal({
      a: { sighting_id: "s1", date: "2026-07-01", thumb_url: "https://x.test/a_thumb.webp", mine: true },
      b: { sighting_id: "s2", date: "2026-07-09", thumb_url: "https://x.test/b_thumb.webp", mine: false },
    })]));
    render(<Review onUnauthorized={() => {}} />);
    expect(await screen.findByText("2026-07-01 · YOUR PHOTO")).toBeInTheDocument();
    expect(screen.getByText("2026-07-09")).toBeInTheDocument();
  });

  it("drops a pair someone else answered first instead of failing the queue", async () => {
    // Everyone works one queue, so a 409 is two people on one pair -- ordinary.
    vi.mocked(getProposals).mockResolvedValue(resp([proposal(), proposal({ id: "p2" })]));
    vi.mocked(resolveProposal).mockRejectedValueOnce(new HttpError(409));
    const onDecided = vi.fn();
    render(<Review onUnauthorized={() => {}} onDecided={onDecided} />);
    await waitFor(() => expect(screen.getAllByText("DIFFERENT ANIMALS")).toHaveLength(2));
    await userEvent.click(screen.getAllByText("DIFFERENT ANIMALS")[0]);
    expect(await screen.findByText(/Someone may have answered it first/)).toBeInTheDocument();
    expect(screen.getAllByText("DIFFERENT ANIMALS")).toHaveLength(1);
    expect(screen.queryByText(/COULDN'T LOAD/)).not.toBeInTheDocument();
    expect(onDecided).toHaveBeenCalled();
  });

  it("tells the caller after each verdict, so the dogs list can refresh", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal()]));
    vi.mocked(resolveProposal).mockResolvedValue(undefined);
    const onDecided = vi.fn();
    render(<Review onUnauthorized={() => {}} onDecided={onDecided} />);
    await userEvent.click(await screen.findByText("DIFFERENT ANIMALS"));
    await waitFor(() => expect(onDecided).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Saved as two different animals.")).toBeInTheDocument();
  });

  it("offers a way back only when opened from somewhere", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal()]));
    const onBack = vi.fn();
    const { unmount } = render(<Review onUnauthorized={() => {}} />);
    await screen.findByText("SAME ANIMAL");
    expect(screen.queryByText("← Back to dogs")).not.toBeInTheDocument();
    unmount();
    render(<Review onUnauthorized={() => {}} onBack={onBack} />);
    await userEvent.click(await screen.findByText("← Back to dogs"));
    expect(onBack).toHaveBeenCalled();
  });

  it("escalates a 401", async () => {
    vi.mocked(getProposals).mockImplementation(async () => {
      throw new UnauthorizedError();
    });
    const onUnauthorized = vi.fn();
    render(<Review onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });
});

describe("Review species wording", () => {
  const side = (species: "dog" | "cat" | null, id: string) => ({ sighting_id: id, date: "2026-07-01", thumb_url: null, species });
  it.each([["cat", "CAT"], ["dog", "DOG"]] as const)("names the %s in the buttons, the confirm and the toast", async (species, upper) => {
    vi.mocked(getProposals).mockResolvedValue(resp([proposal({ a: side(species, "s1"), b: side(species, "s2") })]));
    vi.mocked(resolveProposal).mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Review onUnauthorized={() => {}} />);
    expect(await screen.findByText(`DIFFERENT ${upper}S`)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`SAME ${upper} OR NOT\\?`))).toBeInTheDocument();
    await userEvent.click(screen.getByText(`SAME ${upper}`));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining(`same ${species}?`));
    expect(await screen.findByText(`Saved as one ${species}. You'll find them under DOGS.`)).toBeInTheDocument();
    confirmSpy.mockRestore();
  });
  it("is neutral when the pair, or the list, mixes species or doesn't know", async () => {
    vi.mocked(getProposals).mockResolvedValue(resp([
      proposal({ id: "p1", a: side("cat", "s1"), b: side(null, "s2") }),
      proposal({ id: "p2", a: side("dog", "s3"), b: side("dog", "s4") }),
    ]));
    render(<Review onUnauthorized={() => {}} />);
    expect(await screen.findByText(/SAME ANIMAL OR NOT\?/)).toBeInTheDocument();
    expect(screen.getAllByText("SAME ANIMAL")).toHaveLength(1);
    expect(screen.getAllByText("SAME DOG")).toHaveLength(1);
  });
});
