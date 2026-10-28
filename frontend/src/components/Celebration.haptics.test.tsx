// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { UPLOAD_COMPLETE_EVENT } from "../processing";
import Celebration from "./Celebration";

/** The real haptics helper, with only navigator.vibrate stood in for the phone. */
const calls: { at: number; pattern: number[] }[] = [];
beforeEach(() => {
  calls.length = 0;
  vi.useFakeTimers();
  vi.setSystemTime(Date.now() + 120_000);
  vi.stubGlobal("navigator", { vibrate: (pattern: number[]) => calls.push({ at: Date.now(), pattern }) });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("the confetti buzz on the web", () => {
  it("lets a burst of acknowledgements add taps after the success, never cutting it off", () => {
    render(<Celebration />);
    for (let i = 0; i < 4; i++) act(() => void window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT, { detail: {} })));
    act(() => void vi.advanceTimersByTime(3000));
    expect(calls.map((c) => c.pattern)).toEqual([[30, 130, 14, 106, 14], [8], [8], [8]]);
    const end = (c: { at: number; pattern: number[] }) => c.at + c.pattern.reduce((a, b) => a + b, 0);
    for (let i = 1; i < calls.length; i++) expect(calls[i].at).toBeGreaterThanOrEqual(end(calls[i - 1]));
  });
});
