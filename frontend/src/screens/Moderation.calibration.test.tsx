// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  getDex: vi.fn(), getMe: vi.fn(), getModerationQueue: vi.fn(), getAnimalAudit: vi.fn(), getCalibrationBatches: vi.fn(), createCalibrationBatch: vi.fn(),
  getCalibrationPairs: vi.fn(), getCalibrationDetail: vi.fn(), getCalibrationMetrics: vi.fn(), labelCalibrationPair: vi.fn(), resolveProposal: vi.fn(),
}));
import {
  createCalibrationBatch, getAnimalAudit, getCalibrationBatches, getCalibrationDetail, getCalibrationMetrics,
  getCalibrationPairs, getModerationQueue, HttpError, labelCalibrationPair, resolveProposal, UnauthorizedError,
  type CalibrationBatch, type CalibrationDetail, type CalibrationMetrics, type CalibrationPage,
} from "../api";
import Moderation from "./Moderation";
import Dex from "./Dex";
import { getDex, getMe } from "../api";
import { getCaptures } from "../captureApi";
vi.mock("../captureApi", async (original) => ({ ...(await original<typeof import("../captureApi")>()), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>Map placeholder</div> }));

const batch = (id = "b1"): CalibrationBatch => ({ id, created_at: "2026-10-01T10:00:00Z", eligible_pairs: 1000, sampled_pairs: 100, policy: "uniform", model: "miew", radius_m: 1000 });
const photo = { id: "photo1", photo_url: "full.webp", thumb_url: "thumb.webp", width: 200, height: 100, bbox: [20, 10, 100, 90] as [number, number, number, number] };
const detail = (over: Partial<CalibrationDetail> = {}): CalibrationDetail => ({
  id: "p1", score: 0.123, status: "unreviewed", my_verdict: null, evidence_token: "token-p1",
  left: { sighting_id: "s1", species: "cat", photos: [photo, { ...photo, id: "photo2", photo_url: "second.webp", bbox: null }] },
  right: { sighting_id: "s2", species: null, photos: [photo] }, ...over,
});
const metrics = (over: Partial<CalibrationMetrics> = {}): CalibrationMetrics => ({
  eligible_pairs: 1000, sampled_pairs: 100, reviewed_pairs: 60, same: 25, different: 25, unsure: 5, conflict: 3, stale: 2, unreviewed: 40,
  current_threshold: 0.5, cutoffs: [
    { threshold: 0.2, tp: 20, fp: 10, fn: 5, tn: 15, precision: 2 / 3, recall: 0.8 },
    { threshold: 0.5, tp: 10, fp: 2, fn: 15, tn: 23, precision: 5 / 6, recall: 0.4 },
  ], suggested_threshold: 0.2, suggestion_note: "Exploratory result from this sample.", ...over,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { resolve, promise };
}
async function open(onUnauthorized = vi.fn()) {
  render(<Moderation onUnauthorized={onUnauthorized} />);
  await userEvent.click(screen.getByRole("button", { name: "MATCH CALIBRATION" }));
}
function loadPhoto(image: HTMLElement) {
  Object.defineProperties(image, {
    naturalWidth: { value: 200, configurable: true }, naturalHeight: { value: 100, configurable: true },
  });
  fireEvent.load(image);
}
function loadCurrentPhotos() {
  screen.queryAllByRole("img", { name: /^(Query|Comparison) animal frame/ }).forEach(loadPhoto);
}
async function openDetail(loadImages = true) {
  await open();
  await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
  await screen.findByText("My saved verdict: Not reviewed");
  if (loadImages) loadCurrentPhotos();
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getModerationQueue).mockResolvedValue({ items: [] });
  vi.mocked(getAnimalAudit).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(getCalibrationBatches).mockResolvedValue({ batches: [batch(), batch("b2")] });
  vi.mocked(createCalibrationBatch).mockResolvedValue(batch("new"));
  vi.mocked(getCalibrationPairs).mockResolvedValue({ items: [detail()], next_offset: null });
  vi.mocked(getCalibrationDetail).mockResolvedValue(detail());
  vi.mocked(getCalibrationMetrics).mockResolvedValue(metrics());
  vi.mocked(labelCalibrationPair).mockImplementation(async (_batch, _pair, verdict) => {
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ my_verdict: verdict, status: verdict }));
    return { status: "ok" };
  });
});
afterEach(cleanup);

describe("moderator match calibration", () => {
  it("requires both current images to load, not all frames, and clears readiness on frame changes and errors", async () => {
    await openDetail(false);
    const same = screen.getByRole("button", { name: "SAME ANIMAL" });
    const different = screen.getByRole("button", { name: "DIFFERENT ANIMALS" });
    expect(same).toBeDisabled();
    expect(different).toBeDisabled();
    expect(screen.getByRole("button", { name: "UNSURE" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Skip — no label" })).toBeEnabled();
    await userEvent.click(same);
    await userEvent.click(different);
    expect(labelCalibrationPair).not.toHaveBeenCalled();
    fireEvent.load(screen.getByRole("img", { name: "Query animal frame 1" }));
    expect(same).toBeDisabled();
    loadPhoto(screen.getByRole("img", { name: "Query animal frame 1" }));
    expect(same).toBeDisabled();
    loadPhoto(screen.getByRole("img", { name: "Comparison animal frame 1" }));
    expect(same).toBeEnabled();
    expect(different).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Query animal next frame" }));
    expect(same).toBeDisabled();
    loadPhoto(screen.getByRole("img", { name: "Query animal frame 2" }));
    expect(same).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Query animal previous frame" }));
    expect(same).toBeDisabled();
    loadPhoto(screen.getByRole("img", { name: "Query animal frame 1" }));
    expect(same).toBeEnabled();
    fireEvent.error(screen.getByRole("img", { name: "Comparison animal frame 1" }));
    expect(same).toBeDisabled();
    expect(different).toBeDisabled();
    await userEvent.click(different);
    expect(labelCalibrationPair).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "UNSURE" }));
    expect(labelCalibrationPair).toHaveBeenCalledWith("b1", "p1", "unsure", "token-p1", expect.any(AbortSignal));
  });

  it("keeps decisive labels disabled when either side has no photos", async () => {
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ right: { sighting_id: "s2", species: null, photos: [] } }));
    await openDetail();
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "DIFFERENT ANIMALS" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "UNSURE" })).toBeEnabled();
  });

  it("hides stale evidence discovered by refreshing expired photo URLs", async () => {
    await openDetail();
    fireEvent.error(screen.getByRole("img", { name: "Query animal frame 1" }));
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ status: "stale" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry photo" }));
    expect(await screen.findByText(/create a fresh batch/)).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Comparison animal frame 1" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "UNSURE" })).toBeDisabled();
  });

  it("reconciles selection, metadata, metrics and evidence when an offset page shifts", async () => {
    vi.mocked(getCalibrationBatches).mockResolvedValueOnce({ batches: [batch()], next_offset: 20 })
      .mockResolvedValueOnce({ batches: [batch("old1"), batch("old2")], next_offset: 40 })
      .mockResolvedValue({ batches: [{ ...batch("shifted"), model: "shifted-model", radius_m: 321 }], next_offset: 40 });
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Older batches" }));
    await userEvent.selectOptions(await screen.findByRole("combobox"), "old2");
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    await screen.findByText("My saved verdict: Not reviewed");
    expect(getCalibrationDetail).toHaveBeenLastCalledWith("old2", "p1", expect.any(AbortSignal));
    await userEvent.click(screen.getByRole("button", { name: "Refresh past batches" }));
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("shifted"));
    expect(screen.getByText(/Model: shifted-model.*Radius: 321 m/)).toBeInTheDocument();
    expect(getCalibrationPairs).toHaveBeenLastCalledWith("shifted", 0, expect.any(AbortSignal));
    expect(getCalibrationMetrics).toHaveBeenLastCalledWith("shifted", expect.any(AbortSignal));
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    await screen.findByText("My saved verdict: Not reviewed");
    expect(getCalibrationDetail).toHaveBeenLastCalledWith("shifted", "p1", expect.any(AbortSignal));
    loadCurrentPhotos();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(labelCalibrationPair).toHaveBeenLastCalledWith("shifted", "p1", "same", "token-p1", expect.any(AbortSignal));
  });

  it("refetches saved evidence on reentry after unavoidable app unmount", async () => {
    const pending = deferred<{ status: "ok" }>();
    vi.mocked(labelCalibrationPair).mockReturnValue(pending.promise);
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    const writeSignal = vi.mocked(labelCalibrationPair).mock.calls[0][4]!;
    cleanup();
    expect(writeSignal.aborted).toBe(false);
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ my_verdict: "same", status: "same" }));
    await act(async () => pending.resolve({ status: "ok" }));
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    expect(await screen.findByText("My saved verdict: same")).toBeInTheDocument();
    expect(getCalibrationBatches).toHaveBeenCalledTimes(2);
    expect(getCalibrationDetail).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeDisabled();
  });

  it("reaches older batches beyond the initial page", async () => {
    vi.mocked(getCalibrationBatches).mockImplementation(async (_signal, offset = 0) => offset
      ? { batches: [batch("old")], next_offset: null }
      : { batches: [batch()], next_offset: 20 });
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Older batches" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Review batch" })).toHaveValue("old"));
    expect(getCalibrationBatches).toHaveBeenLastCalledWith(expect.any(AbortSignal), 20);
    await userEvent.click(screen.getByRole("button", { name: "Newer batches" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Review batch" })).toHaveValue("b1"));
  });

  it("explains non-retryable corpus limits", async () => {
    vi.mocked(createCalibrationBatch).mockRejectedValue(new HttpError(422, "calibration corpus exceeds 300 eligible sightings"));
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Create frozen review batch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("calibration corpus exceeds 300 eligible sightings");
    expect(screen.getByRole("alert")).toHaveTextContent("retrying alone will not help");
  });

  it("uses natural image dimensions when legacy metadata is null", async () => {
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({
      left: { sighting_id: "s1", species: "dog", photos: [{ ...photo, width: null, height: null }] },
    }));
    await openDetail();
    const image = screen.getByRole("img", { name: "Query animal frame 1" });
    Object.defineProperties(image, { naturalWidth: { value: 200 }, naturalHeight: { value: 100 } });
    fireEvent.load(image);
    const query = screen.getByRole("region", { name: "Query animal" });
    expect(within(query).getByRole("img", { name: "Animal focus box" })).toHaveStyle({ left: "10%", top: "10%", width: "40%", height: "80%" });
  });

  it.each([false, true])("keeps the calibration entry moderator-only (moderator=%s)", async (isModerator) => {
    vi.mocked(getDex).mockResolvedValue({ sightings: [] });
    vi.mocked(getMe).mockResolvedValue({ is_moderator: isModerator } as Awaited<ReturnType<typeof getMe>>);
    vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
    render(<Dex onUnauthorized={vi.fn()} />);
    await screen.findByRole("button", { name: "JOURNAL" });
    if (isModerator) {
      await userEvent.click(await screen.findByRole("button", { name: "FLAGS" }));
      await userEvent.click(screen.getByRole("button", { name: "MATCH CALIBRATION" }));
      expect(await screen.findByRole("button", { name: "Review pair 1" })).toBeInTheDocument();
    } else {
      expect(screen.queryByRole("button", { name: "FLAGS" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "MATCH CALIBRATION" })).not.toBeInTheDocument();
      expect(getCalibrationBatches).not.toHaveBeenCalled();
    }
  });
  it("keeps existing tabs and only loads calibration when selected", async () => {
    render(<Moderation onUnauthorized={vi.fn()} />);
    expect(getCalibrationBatches).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "REPORTED" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "DETECTOR AUDIT" }));
    expect(await screen.findByRole("region", { name: "Detector audit" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "MATCH CALIBRATION" }));
    expect(await screen.findByRole("region", { name: "Match calibration" })).toBeInTheDocument();
    expect(await screen.findByText(/Model score: 0.123/)).toBeInTheDocument();
  });

  it("creates a frozen batch with default 100, validates limits, and selects it", async () => {
    await open();
    const input = screen.getByRole("spinbutton", { name: "Sample size (1–500)" });
    expect(input).toHaveValue(100);
    for (const value of ["0", "501", "2.5", ""]) {
      fireEvent.change(input, { target: { value } });
      expect(screen.getByRole("button", { name: "Create frozen review batch" })).toBeDisabled();
    }
    fireEvent.change(input, { target: { value: "100" } });
    await userEvent.click(screen.getByRole("button", { name: "Create frozen review batch" }));
    expect(createCalibrationBatch).toHaveBeenCalledWith(100, expect.any(AbortSignal));
    expect(screen.getByRole("combobox", { name: "Review batch" })).toHaveValue("new");
    expect(getCalibrationPairs).toHaveBeenLastCalledWith("new", 0, expect.any(AbortSignal));
  });

  it("paginates pairs and resets pagination when selecting a past batch", async () => {
    vi.mocked(getCalibrationPairs).mockResolvedValueOnce({ items: [detail()], next_offset: 20 })
      .mockResolvedValue({ items: [detail({ id: "p21", score: -0.1 })], next_offset: null });
    await open();
    await screen.findByRole("button", { name: "Review pair 1" });
    await userEvent.click(screen.getByRole("button", { name: "Next pairs" }));
    expect(await screen.findByRole("button", { name: "Review pair 21" })).toBeInTheDocument();
    expect(screen.getByText(/Model score: -0.100/)).toBeInTheDocument();
    expect(getCalibrationPairs).toHaveBeenLastCalledWith("b1", 20, expect.any(AbortSignal));
    await userEvent.click(screen.getByRole("button", { name: "Previous pairs" }));
    expect(getCalibrationPairs).toHaveBeenLastCalledWith("b1", 0, expect.any(AbortSignal));
    await userEvent.selectOptions(screen.getByRole("combobox"), "b2");
    expect(getCalibrationPairs).toHaveBeenLastCalledWith("b2", 0, expect.any(AbortSignal));
    expect(getCalibrationMetrics).toHaveBeenLastCalledWith("b2", expect.any(AbortSignal));
  });

  it("formats known precision/recall and confusion counts, highlights current cutoff and explains sampling", async () => {
    await open();
    const table = await screen.findByRole("table");
    const row = within(table).getByRole("rowheader", { name: "0.5 (current)" }).closest("tr")!;
    expect(row).toHaveClass("calibration-current");
    expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["10", "2", "15", "23", "83.3%", "40.0%"]);
    expect(within(table).getByText("66.7%")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Recall on reviewed pairs" })).toBeInTheDocument();
    expect(screen.getByText(/uniform sample.*below the current cutoff/)).toBeInTheDocument();
    expect(screen.getByText(/not historical replay or street-wide accuracy/)).toBeInTheDocument();
    expect(screen.getByText("Exploratory suggested cutoff: 0.2.")).toBeInTheDocument();
    expect(screen.getByText("Exploratory result from this sample.")).toBeInTheDocument();
  });

  it("renders undefined metrics as insufficient labels, not zero or perfect accuracy", async () => {
    vi.mocked(getCalibrationMetrics).mockResolvedValue(metrics({ same: 0, different: 0, suggested_threshold: null,
      cutoffs: [{ threshold: 0.5, tp: 0, fp: 0, fn: 0, tn: 0, precision: null, recall: null }] }));
    await open();
    const table = await screen.findByRole("table");
    expect(within(table).getAllByText("Not enough labels")).toHaveLength(2);
    expect(within(table).queryByText("0.0%")).not.toBeInTheDocument();
    expect(within(table).queryByText("100.0%")).not.toBeInTheDocument();
    expect(screen.getByText("Exploratory suggested cutoff: Not enough labels.")).toBeInTheDocument();
  });

  it("shows species, image-relative focus boxes, every frame, and image retry", async () => {
    await openDetail();
    expect(screen.getByText("Species: cat")).toBeInTheDocument();
    expect(screen.getByText("Species: Unknown")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Animal focus box" })[0]).toHaveStyle({ left: "10%", top: "10%", width: "40%", height: "80%" });
    await userEvent.click(screen.getByRole("button", { name: "Query animal next frame" }));
    expect(screen.getByRole("img", { name: "Query animal frame 2" })).toHaveAttribute("src", "second.webp");
    expect(screen.getByText(/No focus box available/)).toBeInTheDocument();
    fireEvent.error(screen.getByRole("img", { name: "Query animal frame 2" }));
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({
      left: { sighting_id: "s1", species: "cat", photos: [{ ...photo, photo_url: "fresh-signed.webp" }] },
    }));
    await userEvent.click(screen.getByRole("button", { name: "Retry photo" }));
    expect(await screen.findByRole("img", { name: "Query animal frame 1" })).toHaveAttribute("src", "fresh-signed.webp");
    expect(getCalibrationDetail).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeDisabled();
    loadPhoto(screen.getByRole("img", { name: "Query animal frame 1" }));
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeDisabled();
    loadPhoto(screen.getByRole("img", { name: "Comparison animal frame 1" }));
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeEnabled();
  });

  it("saves and reverses my verdict, refetches metrics and pair state, never resolves identities", async () => {
    await openDetail();
    for (const [name, verdict] of [["SAME ANIMAL", "same"], ["DIFFERENT ANIMALS", "different"], ["UNSURE", "unsure"]] as const) {
      loadCurrentPhotos();
      await userEvent.click(screen.getByRole("button", { name }));
      expect(labelCalibrationPair).toHaveBeenLastCalledWith("b1", "p1", verdict, "token-p1", expect.any(AbortSignal));
      expect(await screen.findByText(`My saved verdict: ${verdict}`)).toBeInTheDocument();
    }
    expect(getCalibrationMetrics).toHaveBeenCalledTimes(4);
    expect(getCalibrationPairs).toHaveBeenCalledTimes(4);
    expect(resolveProposal).not.toHaveBeenCalled();
  });

  it("skip advances without a label, separate from explicitly saving unsure", async () => {
    vi.mocked(getCalibrationPairs).mockResolvedValue({ items: [detail(), detail({ id: "p2" })], next_offset: null });
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "Skip — no label" }));
    expect(getCalibrationDetail).toHaveBeenLastCalledWith("b1", "p2", expect.any(AbortSignal));
    expect(labelCalibrationPair).not.toHaveBeenCalled();
    expect(resolveProposal).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole("button", { name: "UNSURE" }));
    expect(labelCalibrationPair).toHaveBeenCalledWith("b1", "p2", "unsure", "token-p1", expect.any(AbortSignal));
  });

  it("advances to the next pair while the post-save list refresh is pending", async () => {
    const pending = deferred<CalibrationPage>();
    vi.mocked(getCalibrationPairs).mockResolvedValueOnce({ items: [detail(), detail({ id: "p2" })], next_offset: null }).mockReturnValueOnce(pending.promise);
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    await screen.findByText("My saved verdict: same");
    await userEvent.click(screen.getByRole("button", { name: "Skip — no label" }));
    expect(getCalibrationDetail).toHaveBeenLastCalledWith("b1", "p2", expect.any(AbortSignal));
    await act(async () => pending.resolve({ items: [detail(), detail({ id: "p2" })], next_offset: null }));
  });

  it("disables stale evidence and treats disagreement as a separate revisable conflict", async () => {
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ status: "stale" }));
    await openDetail();
    expect(screen.getByText(/create a fresh batch/)).toBeInTheDocument();
    for (const name of ["SAME ANIMAL", "DIFFERENT ANIMALS", "UNSURE"]) expect(screen.getByRole("button", { name })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Back to pairs" }));
    vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ status: "conflict" }));
    await userEvent.click(screen.getByRole("button", { name: "Review pair 1" }));
    expect(await screen.findByText(/Moderators disagree/)).toBeInTheDocument();
    loadCurrentPhotos();
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeEnabled();
    expect(screen.queryByText(/create a fresh batch/)).not.toBeInTheDocument();
  });

  it("locks verdicts after 409, explains a fresh batch and refreshes metrics", async () => {
    vi.mocked(labelCalibrationPair).mockRejectedValue(new HttpError(409));
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(await screen.findByText(/create a fresh batch/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "UNSURE" })).toBeDisabled();
    expect(screen.queryByRole("img", { name: "Query animal frame 1" })).not.toBeInTheDocument();
    expect(screen.getByText("My saved verdict: Not reviewed")).toBeInTheDocument();
    expect(getCalibrationMetrics).toHaveBeenCalledTimes(2);
  });

  it("reloads a verdict after a lost save response without another write", async () => {
    vi.mocked(labelCalibrationPair).mockImplementationOnce(async () => {
      vi.mocked(getCalibrationDetail).mockResolvedValue(detail({ status: "same", my_verdict: "same" }));
      throw new Error("response lost after commit");
    });
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("server may have saved it");
    await userEvent.click(screen.getByRole("button", { name: "Reload saved verdict" }));
    expect(await screen.findByText("My saved verdict: same")).toBeInTheDocument();
    expect(labelCalibrationPair).toHaveBeenCalledTimes(1);
  });

  it("prevents duplicate writes and navigation during a pending label", async () => {
    const pending = deferred<{ status: "ok" }>();
    vi.mocked(labelCalibrationPair).mockReturnValue(pending.promise);
    vi.mocked(getCalibrationBatches).mockResolvedValue({ batches: [batch(), batch("b2")], next_offset: 40 });
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Older batches" }));
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    await screen.findByText("My saved verdict: Not reviewed");
    loadCurrentPhotos();
    await userEvent.dblClick(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(labelCalibrationPair).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Skip — no label" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Back to pairs" })).toBeDisabled();
    for (const name of ["Create frozen review batch", "Refresh past batches", "Newer batches", "Older batches", "REPORTED", "DETECTOR AUDIT", "MATCH CALIBRATION", "Query animal next frame"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
      await userEvent.click(screen.getByRole("button", { name }));
    }
    const select = screen.getByRole("combobox", { name: "Review batch" });
    expect(select).toBeDisabled();
    await userEvent.selectOptions(select, "b2");
    expect(select).toHaveValue("b1");
    expect(screen.getByRole("spinbutton", { name: "Sample size (1–500)" })).toBeDisabled();
    expect(createCalibrationBatch).not.toHaveBeenCalled();
    expect(getCalibrationBatches).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("region", { name: "Pair evidence" })).toBeInTheDocument();
    await act(async () => pending.resolve({ status: "ok" }));
    expect(select).toBeEnabled();
    expect(screen.getByRole("button", { name: "REPORTED" })).toBeEnabled();
    expect(await screen.findByText(/Label saved/)).toBeInTheDocument();
  });

  it("recovers from batch, pairs, metrics and detail read errors", async () => {
    vi.mocked(getCalibrationBatches).mockRejectedValueOnce(new Error("network"));
    vi.mocked(getCalibrationPairs).mockRejectedValueOnce(new Error("network"));
    vi.mocked(getCalibrationMetrics).mockRejectedValueOnce(new Error("network"));
    vi.mocked(getCalibrationDetail).mockRejectedValueOnce(new Error("network"));
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Retry batches" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry pairs" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry metrics" }));
    await screen.findByRole("table");
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry evidence" }));
    await screen.findByRole("button", { name: "SAME ANIMAL" });
    loadCurrentPhotos();
    expect(screen.getByRole("button", { name: "SAME ANIMAL" })).toBeEnabled();
  });

  it("recovers from creation and label errors without claiming success", async () => {
    vi.mocked(createCalibrationBatch).mockRejectedValueOnce(new Error("network"));
    vi.mocked(labelCalibrationPair).mockRejectedValueOnce(new Error("network"));
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Create frozen review batch" }));
    expect(await screen.findByText(/Couldn't create the batch/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create frozen review batch" }));
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    await screen.findByRole("button", { name: "SAME ANIMAL" });
    loadCurrentPhotos();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(await screen.findByText(/Couldn't save the label/)).toBeInTheDocument();
    expect(screen.queryByText(/Label saved/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(await screen.findByText("My saved verdict: same")).toBeInTheDocument();
  });

  it("ignores stale batch-page and metrics responses after batch selection", async () => {
    const page = deferred<CalibrationPage>();
    const oldMetrics = deferred<CalibrationMetrics>();
    vi.mocked(getCalibrationPairs).mockReturnValueOnce(page.promise);
    vi.mocked(getCalibrationMetrics).mockReturnValueOnce(oldMetrics.promise);
    await open();
    await userEvent.selectOptions(await screen.findByRole("combobox"), "b2");
    const signal = vi.mocked(getCalibrationPairs).mock.calls[0][2]!;
    expect(signal.aborted).toBe(true);
    await screen.findByRole("table");
    await act(async () => {
      page.resolve({ items: [detail({ id: "OLD", score: 0.999 })], next_offset: 20 });
      oldMetrics.resolve(metrics({ suggestion_note: "STALE METRICS" }));
    });
    expect(screen.queryByText(/0.999/)).not.toBeInTheDocument();
    expect(screen.queryByText("STALE METRICS")).not.toBeInTheDocument();
  });

  it("cancels obsolete detail reads but blocks tab navigation until a save settles", async () => {
    const old = deferred<CalibrationDetail>();
    vi.mocked(getCalibrationDetail).mockReturnValueOnce(old.promise);
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    const signal = vi.mocked(getCalibrationDetail).mock.calls[0][2]!;
    await userEvent.click(screen.getByRole("button", { name: "Back to pairs" }));
    expect(signal.aborted).toBe(true);
    await act(async () => old.resolve(detail()));
    expect(screen.queryByRole("region", { name: "Pair evidence" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review pair 1" }));
    const pending = deferred<{ status: "ok" }>();
    vi.mocked(labelCalibrationPair).mockReturnValueOnce(pending.promise);
    await screen.findByRole("button", { name: "SAME ANIMAL" });
    loadCurrentPhotos();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    const writeSignal = vi.mocked(labelCalibrationPair).mock.calls[0][4]!;
    const before = vi.mocked(getCalibrationMetrics).mock.calls.length;
    expect(screen.getByRole("button", { name: "REPORTED" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "REPORTED" }));
    expect(writeSignal.aborted).toBe(false);
    expect(screen.getByRole("region", { name: "Pair evidence" })).toBeInTheDocument();
    await act(async () => pending.resolve({ status: "ok" }));
    expect(getCalibrationMetrics).toHaveBeenCalledTimes(before + 1);
    await userEvent.click(screen.getByRole("button", { name: "REPORTED" }));
    expect(screen.queryByRole("region", { name: "Pair evidence" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "MATCH CALIBRATION" }));
    await screen.findByRole("button", { name: "Review pair 1" });
    expect(getCalibrationBatches).toHaveBeenCalledTimes(2);
    expect(getCalibrationMetrics).toHaveBeenCalledTimes(before + 2);
  });

  it("keeps a saved label successful when refreshing metrics fails and allows retry", async () => {
    vi.mocked(getCalibrationMetrics).mockResolvedValueOnce(metrics()).mockRejectedValueOnce(new Error("network")).mockResolvedValue(metrics());
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    expect(await screen.findByText("My saved verdict: same")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Retry metrics" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(labelCalibrationPair).toHaveBeenCalledTimes(1);
  });

  it("deduplicates creation and cancels it when leaving calibration", async () => {
    const pending = deferred<CalibrationBatch>();
    vi.mocked(createCalibrationBatch).mockReturnValue(pending.promise);
    await open();
    await userEvent.dblClick(screen.getByRole("button", { name: "Create frozen review batch" }));
    expect(createCalibrationBatch).toHaveBeenCalledTimes(1);
    const signal = vi.mocked(createCalibrationBatch).mock.calls[0][1]!;
    await userEvent.click(screen.getByRole("button", { name: "REPORTED" }));
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(batch("late")));
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("does not offer a suggested cutoff before both label floors are reached", async () => {
    vi.mocked(getCalibrationMetrics).mockResolvedValue(metrics({ same: 19, different: 25, suggested_threshold: 0.2 }));
    await open();
    expect(await screen.findByText("Exploratory suggested cutoff: Not enough labels.")).toBeInTheDocument();
  });

  it("forwards unauthorized label writes to the session handler", async () => {
    const unauthorized = vi.fn();
    vi.mocked(labelCalibrationPair).mockRejectedValue(new UnauthorizedError());
    await open(unauthorized);
    await userEvent.click(await screen.findByRole("button", { name: "Review pair 1" }));
    await screen.findByRole("button", { name: "SAME ANIMAL" });
    loadCurrentPhotos();
    await userEvent.click(screen.getByRole("button", { name: "SAME ANIMAL" }));
    await waitFor(() => expect(unauthorized).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/Label saved/)).not.toBeInTheDocument();
  });

  it("forwards unauthorized reads to the session handler", async () => {
    const unauthorized = vi.fn();
    vi.mocked(getCalibrationBatches).mockRejectedValue(new UnauthorizedError());
    await open(unauthorized);
    await waitFor(() => expect(unauthorized).toHaveBeenCalledTimes(1));
  });
});
