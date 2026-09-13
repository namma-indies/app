import { useCallback, useEffect, useRef, useState } from "react";
import { UnauthorizedError, type ProcessingState } from "./api";
import { isProcessing, UPLOAD_COMPLETE_EVENT } from "./processing";

const MAX_POLLS = 12;

/** Poll only an actively viewed feed. Stop after twelve checks or any error;
 * the last successful data stays visible and a person can explicitly retry. */
export function useProcessingFeed<T extends { processing_state?: ProcessingState }>(
  load: (signal?: AbortSignal) => Promise<{ sightings: T[] }>,
  enabled: boolean,
  onUnauthorized: () => void,
) {
  const [data, setData] = useState<T[] | null>(null);
  const [error, setError] = useState(false);
  const [paused, setPaused] = useState(false);
  const [revision, setRevision] = useState(0);
  const dataRef = useRef(data);
  dataRef.current = data;
  const needsRefresh = useRef(false);
  const unauthorizedRef = useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;
  const refresh = useCallback(() => {
    needsRefresh.current = true;
    setRevision((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let requestTimeout: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    let stopped = false;
    setError(false);
    setPaused(false);

    function schedule() {
      if (disposed || stopped || document.hidden || inFlight) return;
      if (dataRef.current === null || needsRefresh.current) { void request(); return; }
      if (!dataRef.current.some((s) => isProcessing(s.processing_state))) return;
      if (polls >= MAX_POLLS) { setPaused(true); return; }
      clearTimeout(timer);
      timer = setTimeout(() => { polls++; void request(); }, Math.min(3000 * 2 ** polls, 30000));
    }

    async function request() {
      if (disposed || document.hidden || inFlight) return;
      inFlight = true;
      controller = new AbortController();
      requestTimeout = setTimeout(() => controller?.abort(), 20000);
      try {
        const result = await load(controller.signal);
        if (disposed) return;
        needsRefresh.current = false;
        dataRef.current = result.sightings;
        setData(result.sightings);
        setError(false);
      } catch (err) {
        if (disposed) return;
        stopped = true;
        setError(true);
        if (err instanceof UnauthorizedError) unauthorizedRef.current();
      } finally {
        clearTimeout(requestTimeout);
        inFlight = false;
        if (!disposed) schedule();
      }
    }

    function visibility() {
      clearTimeout(timer);
      if (!document.hidden) schedule();
    }
    schedule();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(requestTimeout);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [enabled, load, revision]);

  useEffect(() => {
    window.addEventListener(UPLOAD_COMPLETE_EVENT, refresh);
    return () => window.removeEventListener(UPLOAD_COMPLETE_EVENT, refresh);
  }, [refresh]);

  return { data, setData, error, paused, refresh };
}
