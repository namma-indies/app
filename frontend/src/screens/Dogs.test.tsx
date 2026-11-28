// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getDogs: vi.fn(),
  // Dogs asks how many matches are waiting on every mount. Empty unless a
  // test says otherwise; the matches block below restores this after itself.
  getProposals: vi.fn(async () => ({ proposals: [], propose_min: 0.4 })),
}));

import { getDogs, getProposals, UnauthorizedError, type Dog, type DogsResponse, type Proposal } from "../api";
import Dogs from "./Dogs";

// See the note in Explore.test.tsx: no shared beforeEach resetting the mock.
afterEach(cleanup);

function dog(over: Partial<Dog> = {}): Dog {
  return {
    id: "d1",
    name: null,
    number: 1,
    company: [],
    first_seen: "2026-07-01",
    last_seen: "2026-07-20",
    sighting_count: 3,
    observer_count: 1,
    seen_by_me: true,
    observers: [],
    photos: ["https://example.test/a_thumb.webp"],
    lat: 12.9716,
    lng: 77.5946,
    precision: "exact",
    cell_m: null,
    tags: ["female", "injured"],
    looks_like: [],
    ...over,
  };
}

function resp(dogs: Dog[], propose_min = 0.71): DogsResponse {
  return { dogs, propose_min };
}

describe("a dog's number", () => {
  it("is the one the server gave them, not their place in this list", async () => {
    // Sorted by last-seen, so position says nothing: No. 47 is first here and
    // would have been "NO. 002" when the number was counted off the list.
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ id: "a", number: 47 }), dog({ id: "b", number: 3 })]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText("— NO. 047 —")).toBeInTheDocument();
    expect(screen.getByText("— NO. 003 —")).toBeInTheDocument();
    expect(screen.queryByText(/NO\. 00[12]/)).not.toBeInTheDocument();
  });

  it("gives way to a name", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ name: "Kalu", number: 47 })]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText("Kalu")).toBeInTheDocument();
    expect(screen.queryByText(/NO\. 047/)).not.toBeInTheDocument();
  });
});

describe("where and how they look", () => {
  it("says where in words, and 'around' for a place that is only a cell", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([
      dog({ id: "a", number: 1, place: "Indiranagar, Bengaluru" }),
      dog({ id: "b", number: 2, place: "Domlur, Bengaluru", precision: "area", cell_m: 1000 }),
    ]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/LAST SEEN IN INDIRANAGAR, BENGALURU/)).toBeInTheDocument();
    expect(screen.getByText(/LAST SEEN AROUND DOMLUR, BENGALURU/)).toBeInTheDocument();
    expect(screen.queryByText(/12\.97/)).not.toBeInTheDocument();
  });

  it("falls back to the coordinates until the place has a name", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ place: null })]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/12\.9716, 77\.5946/)).toBeInTheDocument();
  });

  it("leads with the latest photograph, the rest in a strip", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ photos: ["https://x.test/1.webp", "https://x.test/2.webp", "https://x.test/3.webp"] })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await screen.findByText("— NO. 001 —");
    expect(document.querySelector(".dog-lead")).toHaveAttribute("src", "https://x.test/1.webp");
    expect([...document.querySelectorAll(".dog-strip img")].map((i) => i.getAttribute("src")))
      .toEqual(["https://x.test/2.webp", "https://x.test/3.webp"]);
  });
});

describe("often in the company of", () => {
  const pack = () => [
    dog({ id: "a", number: 47, company: [{ id: "b", number: 52, together: 3 }, { id: "gone", number: 9, together: 1 }] }),
    dog({ id: "b", number: 52, photos: ["https://example.test/b_thumb.webp"], company: [{ id: "a", number: 47, together: 3 }] }),
    dog({ id: "c", number: 60 }),
  ];
  const cardOf = (title: string) => screen.getByText(title).closest(".dog-card") as HTMLElement;

  it("lists who they are photographed with, most often first", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp(pack()));
    render(<Dogs onUnauthorized={() => {}} />);
    await screen.findByText("— NO. 047 —");
    const card = cardOf("— NO. 047 —");
    expect(card.querySelector(".company-head")).toHaveTextContent("OFTEN IN THE COMPANY OF");
    expect([...card.querySelectorAll(".company-name")].map((n) => n.textContent)).toEqual(["NO. 052", "NO. 009"]);
    // How often, but only where "often" is true of them.
    expect([...card.querySelectorAll(".company-times")].map((n) => n.textContent)).toEqual(["3 TIMES"]);
    expect(card.querySelector(".companion img")).toHaveAttribute("src", "https://example.test/b_thumb.webp");
  });

  it("says nothing on a card with no company", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp(pack()));
    render(<Dogs onUnauthorized={() => {}} />);
    await screen.findByText("— NO. 060 —");
    expect(cardOf("— NO. 060 —").querySelector(".company")).toBeNull();
  });

  it("does not call one shared photograph a habit", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([
      dog({ id: "a", number: 47, company: [{ id: "b", number: 52, together: 1 }] }),
      dog({ id: "b", number: 52, company: [{ id: "a", number: 47, together: 1 }] }),
    ]));
    render(<Dogs onUnauthorized={() => {}} />);
    await screen.findByText("— NO. 047 —");
    expect(cardOf("— NO. 047 —").querySelector(".company-head")).toHaveTextContent("SEEN IN THE COMPANY OF");
    expect(document.querySelector(".company-times")).toBeNull();
  });

  it("goes to a companion's card when they are tapped", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp(pack()));
    render(<Dogs onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Go to NO. 052" }));
    expect(cardOf("— NO. 052 —")).toHaveClass("dog-card-marked");
    expect(cardOf("— NO. 047 —")).not.toHaveClass("dog-card-marked");
  });

  it("still names a companion who is not in this list, without a way through", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp(pack()));
    render(<Dogs onUnauthorized={() => {}} />);
    await screen.findByText("NO. 009");
    expect(screen.queryByRole("button", { name: "Go to NO. 009" })).not.toBeInTheDocument();
  });

  it("opens on the card it was asked for", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp(pack()));
    render(<Dogs onUnauthorized={() => {}} focusId="c" />);
    await screen.findByText("— NO. 060 —");
    expect(cardOf("— NO. 060 —")).toHaveClass("dog-card-marked");
  });
});

describe("Dogs", () => {
  it("leads with the number of animals, not the number of sightings", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ id: "a" }), dog({ id: "b" })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/2 INDIES IDENTIFIED/)).toBeInTheDocument());
  });

  it("shows the last-seen coordinates", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/12\.9716, 77\.5946/)).toBeInTheDocument(),
    );
  });

  it("names who else logged it, as the map popup does", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ observer_count: 3, observers: ["Priya", "Aswin"] })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/logged by Priya, Aswin/)).toBeInTheDocument());
  });

  it("does not name you to yourself", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ observers: [] })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/NO\. 001/)).toBeInTheDocument());
    expect(screen.queryByText(/logged by/)).not.toBeInTheDocument();
  });

  it("renders a display name as text -- observers type their own at /join", async () => {
    // The same threat the map popup escapes for. React escapes by
    // construction; this pins that the name never reaches innerHTML.
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ observers: ["<img src=x onerror=alert(1)>"] })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/<img src=x/)).toBeInTheDocument());
    expect(document.querySelector("img[onerror]")).toBeNull();
  });

  it("falls back to an identity number while naming does not exist", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/NO\. 001/)).toBeInTheDocument());
  });

  it("uses the name once one exists", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ name: "Kalu" })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("Kalu")).toBeInTheDocument());
  });

  it("explains an empty catalogue instead of looking broken", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/CONFIRM A MATCH AND THE FIRST ONE APPEARS HERE/)).toBeInTheDocument(),
    );
  });

  it("escalates a 401", async () => {
    vi.mocked(getDogs).mockImplementation(async () => {
      throw new UnauthorizedError();
    });
    const onUnauthorized = vi.fn();
    render(<Dogs onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });
});

describe("Dogs look-alikes", () => {
  it("asks rather than asserts, because look-alikes outscore real matches", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([
        dog({ id: "a", looks_like: [{ id: "b", similarity: 0.62 }] }),
        dog({ id: "b", photos: ["https://example.test/b_thumb.webp"] }),
      ]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/SAME ANIMAL\? — NEEDS A HUMAN/)).toBeInTheDocument());
    // The score is shown so a reviewer can calibrate; it must never be
    // presented as a verdict.
    expect(screen.getByText("0.62")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/MATCH CONFIRMED|SAME DOG$/);
  });

  it.each([["cat", "CAT"], ["dog", "DOG"]] as const)("names the %s when the card is one", async (species, upper) => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ id: "a", species, looks_like: [{ id: "b", similarity: 0.62 }] }), dog({ id: "b" })]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(new RegExp(`SAME ${upper}\\? — NEEDS A HUMAN`))).toBeInTheDocument();
  });

  it("marks scores that clear the review threshold", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp(
        [
          dog({ id: "a", looks_like: [{ id: "b", similarity: 0.80 }] }),
          dog({ id: "b" }),
        ],
        0.71,
      ),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("0.80")).toBeInTheDocument());
    expect(screen.getByText("0.80")).toHaveClass("score-high");
  });

  it("leaves the block out entirely when there is nothing to review", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/NO\. 001/)).toBeInTheDocument());
    expect(screen.queryByText(/NEEDS A HUMAN/)).not.toBeInTheDocument();
  });

  it("survives a look-alike that is not in the returned page", async () => {
    // MAX_DOGS truncates the list; a neighbour can legitimately be missing.
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ id: "a", looks_like: [{ id: "missing", similarity: 0.5 }] })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("0.50")).toBeInTheDocument());
  });
});

// --- precision ---------------------------------------------------------------
// A dog card gathers one animal, its history and its last known place onto a
// single screen, which is the shape someone would want if they meant it harm.
// Full precision is reserved for animals this viewer photographed; the rest are
// collapsed to a grid cell server-side, and the card has to say so.
describe("Dogs location precision", () => {
  it("shows a full coordinate for a dog you have photographed", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ precision: "exact", cell_m: null })]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/LAST SEEN 12\.9716, 77\.5946/)).toBeInTheDocument();
  });

  it("says it is an area, and rounds off the digits, for someone else's dog", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ seen_by_me: false, precision: "area", cell_m: 1000, lat: 12.96712, lng: 77.59407 })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);

    const line = await screen.findByText(/LAST SEEN IN A ~1 KM AREA/);
    expect(line).toBeInTheDocument();
    // Four decimals on a coordinate accurate to a kilometre invites the reader
    // to believe the digits.
    expect(line.textContent).toContain("12.97, 77.59");
    expect(line.textContent).not.toContain("12.9671");
  });

  it("reports the radius the server used rather than a hardcoded one", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ seen_by_me: false, precision: "area", cell_m: 2500 })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/~2\.5 KM AREA/)).toBeInTheDocument();
  });

  it("still says nothing when there is no location at all", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ lat: null, lng: null, precision: "none", cell_m: null })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/NO LOCATION RECORDED/)).toBeInTheDocument();
  });
});

// --- matches waiting ---------------------------------------------------------
// There is no MATCHES tab. The queue opens from the top of this screen, which
// is where a verdict's result shows up.
describe("Dogs matches waiting", () => {
  const pair: Proposal = {
    id: "p1",
    score: 0.44,
    a: { sighting_id: "s1", date: "2026-07-01", thumb_url: null },
    b: { sighting_id: "s2", date: "2026-07-09", thumb_url: null },
  };
  afterEach(() => {
    vi.mocked(getProposals).mockResolvedValue({ proposals: [], propose_min: 0.4 });
  });

  it("says how many pairs are waiting", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    vi.mocked(getProposals).mockResolvedValue({ proposals: [pair, { ...pair, id: "p2" }], propose_min: 0.4 });
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText("2 MATCHES WAITING")).toBeInTheDocument();
  });

  it("shows the way in even before the first dog exists", async () => {
    // The first dog only appears once someone answers a match, so an empty
    // catalogue is exactly when this button matters most.
    vi.mocked(getDogs).mockResolvedValue(resp([]));
    vi.mocked(getProposals).mockResolvedValue({ proposals: [pair], propose_min: 0.4 });
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText("1 MATCH WAITING")).toBeInTheDocument();
    expect(screen.getByText(/NO ANIMALS IDENTIFIED YET/)).toBeInTheDocument();
  });

  it("stays out of the way when nothing is waiting", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await screen.findByText(/NO\. 001/);
    expect(screen.queryByText(/WAITING/)).not.toBeInTheDocument();
  });

  it("opens the pairs in place and goes back to the list", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    vi.mocked(getProposals).mockResolvedValue({ proposals: [pair], propose_min: 0.4 });
    render(<Dogs onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByText("1 MATCH WAITING"));
    expect(await screen.findByText("SAME ANIMAL")).toBeInTheDocument();
    await userEvent.click(screen.getByText("← Back to dogs"));
    expect(await screen.findByText(/1 INDIE IDENTIFIED/)).toBeInTheDocument();
  });

  it("still shows the dogs if the match count fails", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    vi.mocked(getProposals).mockRejectedValue(new Error("offline"));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/1 INDIE IDENTIFIED/)).toBeInTheDocument();
    expect(screen.queryByText(/WAITING/)).not.toBeInTheDocument();
  });
});
