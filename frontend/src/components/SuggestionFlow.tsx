import { useRef, type ReactNode } from "react";
import { useSuggestionFlow } from "../suggestionFlow";
import { fxScope } from "../fxSprites";

/**
 * Wraps a transcript and its suggestions so the shimmer and comets play when
 * they are shown, themed for the animal's species. Mounted per entry, so each
 * opening plays it again, for open and for already-used suggestions alike.
 * Adds no box of its own.
 */
export default function SuggestionFlow({ ids, usedIds, species, children }: { ids: string[]; usedIds?: string[]; species?: string | null; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useSuggestionFlow(ref, ids, usedIds);
  return <div ref={ref} className="fx-scope" {...fxScope(species)}>{children}</div>;
}
