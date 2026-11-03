// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => false, isPluginAvailable: () => false, getPlatform: () => "web" },
}));
vi.mock("@capacitor/app", () => ({ App: { addListener: vi.fn() } }));
vi.mock("./screens/Dex", () => ({ default: () => null }));
vi.mock("./screens/Admin", () => ({ default: () => null }));
vi.mock("./components/Celebration", () => ({ default: () => null }));
vi.mock("./offline/queue", () => ({
  enqueue: vi.fn(async () => {}), flush: vi.fn(async () => {}), failedCount: async () => 0,
  setOnFlushed: () => {}, setOnUnauthorized: () => {},
}));
// Exercise real Capture, /me capability readers and the policy dialog. Only
// photo EXIF and the upload sink are outside this test's boundary.
vi.mock("./api", async (original) => {
  const real = await original<typeof import("./api")>();
  return { ...real, readPhotoMetadata: vi.fn(async () => real.NO_METADATA) };
});

import App from "./App";
import { enqueue } from "./offline/queue";
import { CONSENT_REQUIRED_EVENT } from "./consentEvents";

const policy = { enabled: true, terms_version: "one", privacy_version: "one",
  community_guidelines_version: "one", minimum_age: null };
let accepted: boolean;
let blockedLookups: number;

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});

beforeEach(() => {
  accepted = false; blockedLookups = 0;
  vi.stubEnv("VITE_MULTI_ANIMAL_ENABLED", "true");
  vi.mocked(enqueue).mockClear();
  localStorage.clear();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: (file: File) => `blob:${file.name}` });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    const path = String(url);
    if (path.endsWith("/account/consent")) {
      if (init?.method === "POST") accepted = true;
      return response({ policy, required: !accepted, accepted_at: accepted ? "2026-11-03T00:00:00Z" : null });
    }
    if (path.endsWith("/dex")) return accepted ? response({ sightings: [] }) : response({ detail: "consent_required" }, 428);
    if (path.endsWith("/me")) {
      if (!accepted) { blockedLookups++; return response({ detail: "consent_required" }, 428); }
      return response({ id: "test-observer", display_name: null, is_moderator: false,
        multi_animal_enabled: true, can_share_socially: true, note_language: "en" });
    }
    throw new Error(`Unexpected request: ${path}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

async function acceptPolicies() {
  await userEvent.click(await screen.findByRole("checkbox", { name: /I agree to the Terms of Service and Community Guidelines/ }));
  await userEvent.click(screen.getByRole("checkbox", { name: /I acknowledge the Privacy Policy/ }));
  await userEvent.click(screen.getByRole("button", { name: "Agree and continue" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Please review our policies" })).not.toBeInTheDocument());
}

async function save() {
  await userEvent.click(screen.getByRole("button", { name: /LOG IT/ }));
  const withoutPlace = screen.queryByRole("button", { name: /LOG WITHOUT A PLACE/ });
  if (withoutPlace) await userEvent.click(withoutPlace);
  await waitFor(() => expect(enqueue).toHaveBeenCalledOnce());
  return vi.mocked(enqueue).mock.calls[0][0];
}

it.each(["photo", "video"] as const)("restores the capture contract for a new %s after initial capability lookups get 428", async (kind) => {
  render(<App />);
  await screen.findByRole("dialog", { name: "Please review our policies" });
  await waitFor(() => expect(blockedLookups).toBeGreaterThanOrEqual(3));
  await acceptPolicies();
  const media = new File([`${kind}-bytes`], kind === "photo" ? "one.jpg" : "one.mp4",
    { type: kind === "photo" ? "image/jpeg" : "video/mp4" });
  await userEvent.upload(screen.getByLabelText(kind === "photo" ? "capture photo" : "record clip"), media);
  expect(await screen.findByText(/One upload, separate animal entries/)).toBeInTheDocument();
  expect(await screen.findByLabelText("Share with the Namma Indies team")).not.toBeChecked();
  const saved = await save();
  expect(saved.upload_endpoint).toBe("capture");
  expect(saved.usual_language).toBe("en");
  expect(kind === "photo" ? saved.photos?.[0] : saved.video).toBe(media);
});

it("refreshes capabilities while keeping the real capture photo and note already being edited", async () => {
  accepted = true;
  render(<App />);
  const photo = new File(["draft-photo"], "draft.jpg", { type: "image/jpeg" });
  await userEvent.upload(await screen.findByLabelText("capture photo"), photo);
  await screen.findByText(/One upload, separate animal entries/);
  const note = screen.getByPlaceholderText(/Add a note/);
  await userEvent.type(note, "unfinished field note");
  accepted = false;
  act(() => window.dispatchEvent(new Event(CONSENT_REQUIRED_EVENT)));
  await acceptPolicies();
  expect(screen.getByPlaceholderText(/Add a note/)).toHaveValue("unfinished field note");
  expect(screen.getByAltText("captured animal 1")).toBeInTheDocument();
  const saved = await save();
  expect(saved.upload_endpoint).toBe("capture");
  expect(saved.photos?.[0]).toBe(photo);
  expect(saved.note).toBe("unfinished field note");
  expect(saved.usual_language).toBe("en");
});
