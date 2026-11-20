import { afterEach, describe, expect, it, vi } from "vitest";
import { searchPlaces } from "./api";

afterEach(() => vi.unstubAllGlobals());

function mockSearch(cellM: number | null) {
  const fetch = vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith("/places/config") ? { cell_m: cellM } : { places: [] },
  )));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("place-search location privacy", () => {
  it("sends a shared-map cell centre instead of the precise map centre", async () => {
    const fetch = mockSearch(1000);
    const near = Object.freeze({ lat: 12.345678, lng: 67.891234 });
    await expect(searchPlaces("Example road", near)).resolves.toEqual([]);
    expect(fetch).toHaveBeenNthCalledWith(1, "/places/config", { credentials: "include", cache: "no-store" });
    expect(fetch).toHaveBeenNthCalledWith(2, "/places?q=Example+road&lat=12.34729&lng=67.88811", { credentials: "include" });
    expect(JSON.stringify(fetch.mock.calls)).not.toContain("12.345678");
    expect(JSON.stringify(fetch.mock.calls)).not.toContain("67.891234");
  });

  it("uses the configured size and refreshes it on each submitted search", async () => {
    const fetch = mockSearch(2500);
    await searchPlaces("Example road", { lat: 12.345678, lng: 67.891234 });
    await searchPlaces("Example road", { lat: 12.345679, lng: 67.891235 });
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/places/config", "/places?q=Example+road&lat=12.34055&lng=67.89786",
      "/places/config", "/places?q=Example+road&lat=12.34055&lng=67.89786",
    ]);
  });

  it.each([0, -1, null])("omits bias when the configured grid is disabled or invalid (%s)", async (cellM) => {
    const fetch = mockSearch(cellM);
    await searchPlaces("Example road", { lat: 12.345678, lng: 67.891234 });
    expect(fetch.mock.calls[1][0]).toBe("/places?q=Example+road");
  });

  it("does not fetch bias configuration when there is no centre", async () => {
    const fetch = mockSearch(1000);
    await searchPlaces("Example road");
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/places?q=Example+road", { credentials: "include" });
  });

  it("does not send a precise centre if loading the configuration fails", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
    vi.stubGlobal("fetch", fetch);
    await expect(searchPlaces("Example road", { lat: 12.345678, lng: 67.891234 })).rejects.toThrow();
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/places/config", { credentials: "include", cache: "no-store" });
  });
});
