import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createCalibrationBatch, getCalibrationBatches, getCalibrationDetail, getCalibrationMetrics,
  getCalibrationPairs, HttpError, labelCalibrationPair, UnauthorizedError,
} from "./api";

const ok = (body: unknown = {}) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
afterEach(() => vi.unstubAllGlobals());

describe("match calibration API", () => {
  it("escapes batch and pair identifiers, serializes pagination, and propagates credentials and abort signals", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(ok({ items: [], next_offset: null })));
    vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    await getCalibrationBatches(signal);
    await getCalibrationPairs("batch/a", 20, signal);
    await getCalibrationDetail("batch/a", "pair/b", signal);
    await getCalibrationMetrics("batch/a", signal);
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      expect.stringMatching(/\/moderation\/match-calibration$/),
      expect.stringMatching(/\/match-calibration\/batch%2Fa\/pairs\?offset=20&limit=20$/),
      expect.stringMatching(/\/match-calibration\/batch%2Fa\/pairs\/pair%2Fb$/),
      expect.stringMatching(/\/match-calibration\/batch%2Fa\/metrics$/),
    ]);
    for (const [, init] of fetch.mock.calls) expect(init).toEqual({ credentials: "include", signal });
  });

  it("uses JSON for batch creation and each label, never a proposal or identity route", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(ok({ status: "ok" })));
    vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    await createCalibrationBatch(100, signal);
    expect(fetch.mock.calls[0][1]).toEqual({ method: "POST", credentials: "include", signal,
      headers: { "Content-Type": "application/json" }, body: '{"sample_size":100}' });
    for (const verdict of ["same", "different", "unsure"] as const) {
      await labelCalibrationPair("b1", "p1", verdict, "frozen-token", signal);
      expect(fetch).toHaveBeenLastCalledWith(expect.stringMatching(/\/moderation\/match-calibration\/b1\/pairs\/p1\/label$/), {
        method: "POST", credentials: "include", signal, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verdict, evidence_token: "frozen-token" }),
      });
    }
    expect(fetch.mock.calls.every(([url]) => !url.includes("proposal"))).toBe(true);
  });

  it.each([
    () => getCalibrationBatches(), () => createCalibrationBatch(100), () => getCalibrationPairs("b"),
    () => getCalibrationDetail("b", "p"), () => getCalibrationMetrics("b"), () => labelCalibrationPair("b", "p", "same", "token"),
  ])("preserves authentication failures on every calibration request", async (request) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    await expect(request()).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("preserves stale 409 and other HTTP errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(null, { status: 409 }))
      .mockResolvedValueOnce(new Response(null, { status: 403 })));
    await expect(labelCalibrationPair("b", "p", "unsure", "token")).rejects.toMatchObject({ status: 409 });
    await expect(getCalibrationBatches()).rejects.toBeInstanceOf(HttpError);
  });

  it("does not swallow transport cancellation", async () => {
    const controller = new AbortController();
    controller.abort();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("Aborted", "AbortError")));
    await expect(getCalibrationPairs("b", 0, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  });
});
