import { useRef, type ReactNode } from "react";
import { useSuggestionFlow } from "../suggestionFlow";

/**
 * Wraps a transcript and its suggestions so the glow-and-flow plays once when
 * they appear. Mounted per entry, so opening another entry plays it again.
 * Adds no box of its own.
 */
export default function SuggestionFlow({ ids, children }: { ids: string[]; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useSuggestionFlow(ref, ids);
  return <div ref={ref} className="fx-scope">{children}</div>;
}
