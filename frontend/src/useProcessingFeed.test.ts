// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useProcessingFeed } from "./useProcessingFeed";
import { UPLOAD_COMPLETE_EVENT } from "./processing";
import type { ProcessingState } from "./api";

const response = (processing_state: ProcessingState) => ({ sightings: [{ processing_state }] });
let hidden = false;
beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
const settle = () => act(async () => {});
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
function visibility(value: boolean) {
  hidden = value;
  act(() => document.dispatchEvent(new Event("visibilitychange")));
}

describe("processing feed", () => {
  it("polls a capture with no children until private review becomes available", async () => {
    const load = vi.fn().mockResolvedValueOnce({ sightings: [{ capture_id: "c", processing_state: "processing", sighting_ids: [] }] }).mockResolvedValue({ sightings: [{ capture_id: "c", processing_state: "needs_review", sighting_ids: [] }] });
    const { result } = renderHook(() => useProcessingFeed(load, true, () => {}));
    await settle();
    await advance(3000);
    expect(result.current.data?.[0].processing_state).toBe("needs_review");
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("refreshes a completed feed when returning after a review elsewhere", async () => {
    const load = vi.fn().mockResolvedValue({ sightings: [{ processing_state: "needs_review" }] });
    const { rerender } = renderHook(({ enabled }) => useProcessingFeed(load, enabled, () => {}, true), { initialProps: { enabled: true } });
    await settle();
    rerender({ enabled: false });
    load.mockResolvedValue(response("ready"));
    rerender({ enabled: true });
    await settle();
    expect(load).toHaveBeenCalledTimes(2);
  });
  it("refreshes settled captures when the browser tab becomes visible again", async () => {
    const load = vi.fn().mockResolvedValue({ sightings: [{ processing_state: "needs_review" }] });
    const { result } = renderHook(() => useProcessingFeed(load, true, () => {}, true));
    await settle();
    visibility(true);
    load.mockResolvedValue(response("ready"));
    visibility(false);
    await settle();
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.data?.[0].processing_state).toBe("ready");
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(2);
  });
  it("backs off, refreshes pending records, and stops at ready", async () => {
    const load = vi.fn().mockResolvedValueOnce(response("queued")).mockResolvedValueOnce(response("processing")).mockResolvedValue(response("ready"));
    const { result } = renderHook(() => useProcessingFeed(load, true, () => {}));
    await settle();
    expect(load).toHaveBeenCalledTimes(1);
    await advance(2999);
    expect(load).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(load).toHaveBeenCalledTimes(2);
    await advance(5999);
    expect(load).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(result.current.data).toEqual(response("ready").sightings);
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it.each(["legacy", "ready", "no_animal", "failed"] as ProcessingState[])("does not poll %s", async (state) => {
    const load = vi.fn().mockResolvedValue(response(state));
    renderHook(() => useProcessingFeed(load, true, () => {}));
    await settle();
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("does not load or poll in a hidden tab, and cleans up on unmount", async () => {
    hidden = true;
    const load = vi.fn().mockResolvedValue(response("queued"));
    const { unmount } = renderHook(() => useProcessingFeed(load, true, () => {}));
    await advance(60000);
    expect(load).not.toHaveBeenCalled();
    visibility(false);
    await settle();
    expect(load).toHaveBeenCalledTimes(1);
    visibility(true);
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(1);
    visibility(false);
    await advance(3000);
    expect(load).toHaveBeenCalledTimes(2);
    unmount();
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("caps automatic checks at twelve and allows a manual refresh", async () => {
    const load = vi.fn().mockResolvedValue(response("queued"));
    const { result } = renderHook(() => useProcessingFeed(load, true, () => {}));
    await settle();
    await advance(600000);
    expect(load).toHaveBeenCalledTimes(13);
    expect(result.current.paused).toBe(true);
    await advance(600000);
    expect(load).toHaveBeenCalledTimes(13);
    act(() => result.current.refresh());
    await settle();
    expect(load).toHaveBeenCalledTimes(14);
    expect(result.current.paused).toBe(false);
  });

  it("retains last good data and stops retrying after a network error", async () => {
    const load = vi.fn().mockResolvedValueOnce(response("queued")).mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useProcessingFeed(load, true, () => {}));
    await settle();
    await advance(3000);
    expect(result.current.error).toBe(true);
    expect(result.current.data).toEqual(response("queued").sightings);
    await advance(600000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("refreshes a loaded empty feed when an upload is acknowledged", async () => {
    const load = vi.fn().mockResolvedValueOnce({ sightings: [] }).mockResolvedValue(response("queued"));
    const { result } = renderHook(() => useProcessingFeed(load, true, () => {}));
    await settle();
    act(() => window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT)));
    await settle();
    expect(result.current.data).toEqual(response("queued").sightings);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("stops scheduling when the feed is no longer viewed", async () => {
    const load = vi.fn().mockResolvedValue(response("processing"));
    const { rerender } = renderHook(({ enabled }) => useProcessingFeed(load, enabled, () => {}), { initialProps: { enabled: true } });
    await settle();
    rerender({ enabled: false });
    await advance(60000);
    expect(load).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts a request and clears its timeout on unmount", async () => {
    const load = vi.fn((_signal?: AbortSignal) => new Promise<{ sightings: { processing_state: ProcessingState }[] }>(() => {}));
    const { unmount } = renderHook(() => useProcessingFeed(load, true, () => {}));
    const signal = load.mock.calls[0][0];
    unmount();
    expect(signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});
