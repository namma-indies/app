// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { createUpdateController } from "./appUpdate";
import { isBusy, onBusyChange, useReloadGuard } from "./reloadGuard";

function Recorder() {
  useReloadGuard(true);
  return null;
}
function Draft() {
  useReloadGuard(true);
  return null;
}
// Mirrors VoiceNote -> AddVoiceNote: the recorder hold goes and the unsaved
// note's hold arrives in the same commit.
function Screen({ recording }: { recording: boolean }) {
  return recording ? <Recorder /> : <Draft />;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("recording hands over to an unsaved note with an update pending (#175)", () => {
  it("does not reload in the gap between the two holds", () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    const c = createUpdateController({
      isBusy,
      onBusyChange,
      reload,
      setPrompt: () => {},
      isHidden: () => true,
    });
    const view = render(<Screen recording />);
    c.updateReady(); // busy: prompt, no reload
    act(() => {
      view.rerender(<Screen recording={false} />);
    });
    act(() => {
      vi.runAllTimers();
    });
    expect(reload).not.toHaveBeenCalled();

    // The note is saved/discarded: now it may go.
    act(() => view.unmount());
    act(() => {
      vi.runAllTimers();
    });
    expect(reload).toHaveBeenCalledTimes(1);
    c.dispose();
  });
});
