import { useRef, type ReactNode } from "react";
import { useSuggestionFlow } from "../suggestionFlow";

/**
 * Wraps a transcript and its suggestions so the glow-and-flow plays when they
 * are shown. Mounted per entry, so each opening plays it again, for open and
 * for already-used suggestions alike. Adds no box of its own.
 */
export default function SuggestionFlow({ ids, usedIds, children }: { ids: string[]; usedIds?: string[]; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useSuggestionFlow(ref, ids, usedIds);
  return <div ref={ref} className="fx-scope">{children}</div>;
}
