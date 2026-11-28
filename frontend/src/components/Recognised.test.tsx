// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  getRecognised: vi.fn(),
  markRecognisedSeen: vi.fn(async () => {}),
}));
vi.mock("../haptics", () => ({ haptic: vi.fn() }));

import { getRecognised, markRecognisedSeen, UnauthorizedError, type Recognition } from "../api";
import { haptic } from "../haptics";
import Recognised, { apart, knownLine } from "./Recognised";

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function recognition(over: Partial<Recognition> = {}): Recognition {
  return {
    individual: { id: "d47", number: 47, name: null, species: "dog" },
    // Microseconds, as the server sends them.
    recognised_at: "2026-11-27T10:15:30.123456+00:00",
    yours: { id: "s2", captured_at: "2026-11-12T09:00:00+00:00", thumb_url: "https://x.test/now.webp" },
    other: { captured_at: "2026-03-03T09:00:00+00:00", thumb_url: "https://x.test/then.webp", mine: true, observer: null },
    met_count: 2,
    sighting_count: 2,
    observers: [],
    ...over,
  };
}

function show(recognised: Recognition[], more = 0) {
  vi.mocked(getRecognised).mockResolvedValue({ recognised, more });
  const onSee = vi.fn();
  const view = render(<Recognised onSee={onSee} onUnauthorized={() => {}} />);
  return { onSee, ...view };
}

describe("apart", () => {
  it("uses the largest unit that reads naturally", () => {
    const from = "2026-03-03T09:00:00Z";
    expect(apart(from, "2026-03-03T18:00:00Z")).toBe("THE SAME DAY");
    expect(apart(from, "2026-03-04T09:00:00Z")).toBe("1 DAY APART");
    expect(apart(from, "2026-03-12T09:00:00Z")).toBe("9 DAYS APART");
    expect(apart(from, "2026-04-07T09:00:00Z")).toBe("5 WEEKS APART");
    expect(apart(from, "2026-11-12T09:00:00Z")).toBe("8 MONTHS APART");
    expect(apart(from, "2029-03-03T09:00:00Z")).toBe("3 YEARS APART");
    // Order does not matter: it is a distance.
    expect(apart("2026-11-12T09:00:00Z", from)).toBe("8 MONTHS APART");
  });
});

describe("knownLine", () => {
  it("says how often you have met and who else has", () => {
    expect(knownLine(recognition({ met_count: 1, observers: [] }))).toBe("");
    expect(knownLine(recognition({ met_count: 3 }))).toBe("You have met them 3 times.");
    expect(knownLine(recognition({ met_count: 1, observers: ["Priya"] }))).toBe("Priya knows them too.");
    expect(knownLine(recognition({ met_count: 2, observers: ["Priya", "Aswin"] })))
      .toBe("You have met them 2 times. Priya and Aswin know them too.");
    expect(knownLine(recognition({ met_count: 1, observers: ["A", "B", "C"] }))).toBe("3 other people know them too.");
  });
});

describe("Recognised", () => {
  it("shows nothing when nothing has been recognised", async () => {
    const { container } = show([]);
    await waitFor(() => expect(getRecognised).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing, and no error, when it cannot be loaded", async () => {
    vi.mocked(getRecognised).mockRejectedValue(new Error("offline"));
    const { container } = render(<Recognised onSee={() => {}} onUnauthorized={() => {}} />);
    await waitFor(() => expect(getRecognised).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("signs out when the session has gone", async () => {
    vi.mocked(getRecognised).mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();
    render(<Recognised onSee={() => {}} onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });

  it("says who your sighting turned out to be, then and now", async () => {
    show([recognition()]);
    expect(await screen.findByText("RECOGNISED")).toBeInTheDocument();
    expect(screen.getByText(/Your sighting from 12 Nov is/)).toBeInTheDocument();
    expect(screen.getByText("— NO. 047 —")).toBeInTheDocument();
    expect(screen.getByText("THE SAME DOG · 8 MONTHS APART")).toBeInTheDocument();
    expect(screen.getByText("You have met them 2 times.")).toBeInTheDocument();
    // Earlier photograph on the left, whichever of the two is "yours".
    const photos = [...document.querySelectorAll(".recognised-side img")].map((i) => i.getAttribute("src"));
    expect(photos).toEqual(["https://x.test/then.webp", "https://x.test/now.webp"]);
    expect(haptic).toHaveBeenCalledWith("filled");
  });

  it("names whoever took the other photograph, and never calls a cat a dog", async () => {
    show([recognition({
      individual: { id: "d9", number: 9, name: "Kalu", species: null },
      other: { captured_at: "2026-11-20T09:00:00+00:00", thumb_url: null, mine: false, observer: "Priya" },
      met_count: 1, observers: ["Priya"],
    })]);
    expect(await screen.findByText("Kalu")).toBeInTheDocument();
    expect(screen.getByText("THE SAME ANIMAL · 8 DAYS APART")).toBeInTheDocument();
    expect(screen.getByText("PRIYA")).toBeInTheDocument();
    expect(screen.getByText("Priya knows them too.")).toBeInTheDocument();
    // Yours is the earlier one here, so it comes first.
    const who = [...document.querySelectorAll(".recognised-side figcaption span")].map((s) => s.textContent);
    expect(who).toEqual(["YOU", "PRIYA"]);
  });

  it("goes through them oldest first, reading exactly as far as each card", async () => {
    // The server sends newest first.
    const newer = recognition({ individual: { id: "d48", number: 48, name: null, species: "dog" },
      recognised_at: "2026-11-27T12:00:00.000002+00:00" });
    const older = recognition({ recognised_at: "2026-11-26T08:00:00.000001+00:00" });
    show([newer, older], 3);

    expect(await screen.findByText("— NO. 047 —")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    await userEvent.click(screen.getByText("NEXT"));
    // The timestamp goes back exactly as it came: a Date would drop the
    // microseconds and the card would return on every open.
    expect(markRecognisedSeen).toHaveBeenLastCalledWith("2026-11-26T08:00:00.000001+00:00");

    expect(screen.getByText("— NO. 048 —")).toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(screen.getByText(/3 more were recognised before these/)).toBeInTheDocument();
    await userEvent.click(screen.getByText("DONE"));
    expect(markRecognisedSeen).toHaveBeenLastCalledWith("2026-11-27T12:00:00.000002+00:00");
    expect(screen.queryByText("RECOGNISED")).not.toBeInTheDocument();
    expect(haptic).toHaveBeenCalledTimes(2);
  });

  it("takes you to their card, and counts that as read", async () => {
    const { onSee } = show([recognition()]);
    await userEvent.click(await screen.findByText("SEE THEM"));
    expect(onSee).toHaveBeenCalledWith("d47");
    expect(markRecognisedSeen).toHaveBeenCalledWith("2026-11-27T10:15:30.123456+00:00");
  });

  it("is not undone by a failure to mark it read", async () => {
    vi.mocked(markRecognisedSeen).mockRejectedValueOnce(new Error("offline"));
    show([recognition()]);
    await userEvent.click(await screen.findByText("DONE"));
    expect(screen.queryByText("RECOGNISED")).not.toBeInTheDocument();
  });
});
