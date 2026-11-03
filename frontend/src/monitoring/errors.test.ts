// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { startErrorMonitoring } from "./errors";

let stop = () => {};
afterEach(() => {
  stop();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("self-hosted error signals", () => {
  it("stays off until enabled", () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    stop = startErrorMonitoring(false);
    window.dispatchEvent(new ErrorEvent("error", { message: "private note" }));
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("sends a fixed category and throttles a burst without sending private context", () => {
    const fetcher = vi.fn().mockResolvedValue({ status: 204 });
    vi.stubGlobal("fetch", fetcher);
    stop = startErrorMonitoring(true);
    window.dispatchEvent(new ErrorEvent("error", {
      message: "private note", filename: "https://private.example.test/photo/secret", error: new Error("private"),
    }));
    window.dispatchEvent(new Event("unhandledrejection"));
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith("/monitoring/client-error", expect.objectContaining({
      credentials: "same-origin", referrerPolicy: "no-referrer", body: '{"kind":"error"}',
    }));
    expect(JSON.stringify(fetcher.mock.calls)).not.toContain("private");
  });

  it("ignores offline errors and swallows delivery failures", async () => {
    const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    const fetcher = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetcher);
    stop = startErrorMonitoring(true);
    window.dispatchEvent(new Event("unhandledrejection"));
    expect(fetcher).not.toHaveBeenCalled();
    online.mockReturnValue(true);
    window.dispatchEvent(new Event("unhandledrejection"));
    await Promise.resolve();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].body).toBe('{"kind":"unhandled_rejection"}');
  });

  it("removes handlers at shutdown", () => {
    const fetcher = vi.fn().mockResolvedValue({ status: 204 });
    vi.stubGlobal("fetch", fetcher);
    stop = startErrorMonitoring(true);
    stop();
    window.dispatchEvent(new Event("unhandledrejection"));
    expect(fetcher).not.toHaveBeenCalled();
  });
});
