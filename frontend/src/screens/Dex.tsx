import { useEffect, useMemo, useState } from "react";
import {
  getDex,
  getMap,
  getMe,
  setSightingShared,
  UnauthorizedError,
  isUsed,
  notePending,
  type Sighting,
} from "../api";
import AddVoiceNote, { laterNotesOffered, type LaterNotesConfig } from "../components/AddVoiceNote";
import SuggestedDetails from "../components/SuggestedDetails";
import Transcript from "../components/Transcript";
import SuggestionFlow from "../components/SuggestionFlow";
import DogMap from "../components/DogMap";
import CaptureReview from "../components/CaptureReview";
import { getCaptures, type CaptureSummary } from "../captureApi";
import ReportSheet from "../components/ReportSheet";
import { speciesNoun } from "../species";
import Dogs from "./Dogs";
import { useProcessingFeed } from "../useProcessingFeed";
import { isProcessing, processingLabel } from "../processing";
import { formatWhen } from "../formatWhen";

const TAG_LABELS: Record<string, string> = {
  male: "♂ MALE",
  female: "♀ FEMALE",
  left: "NOTCH-L",
  right: "NOTCH-R",
  healthy: "HEALTHY",
  injured: "INJURED",
};

/** The details shown as tags, with the field each one is, so a used suggestion can flow into its own. */
function attrTagFields(s: Sighting): { field: "sex" | "ear_notch" | "condition"; label: string }[] {
  const attrs = s.attrs || {};
  return (["sex", "ear_notch", "condition"] as const)
    .map((field) => ({ field, value: attrs[field] as string | undefined }))
    .filter(({ value }) => !!value && value !== "unsure" && value !== "none")
    .map(({ field, value }) => ({ field, label: TAG_LABELS[value!] ?? value! }));
}

function attrTags(s: Sighting): string[] {
  return attrTagFields(s).map((t) => t.label);
}

function where(s: Sighting): string {
  if (s.lat != null && s.lng != null) return `${s.lat.toFixed(3)}, ${s.lng.toFixed(3)}`;
  return "no GPS";
}

const SUGGESTION_NAMES = { sex: "Sex", ear_notch: "Ear notch", condition: "Condition", known_name: "Name" } as const;

/** A sighting worth checking back for: its media, or a note added to it, is still being worked on. */
const ownBusy = (s: Sighting) => isProcessing(s.processing_state) || (s.later_notes ?? []).some(notePending);

const UNFINISHED = new Set<CaptureSummary["processing_state"]>(["queued", "processing", "failed", "needs_review"]);

/** An upload that is not a sighting yet. One that needs its contributor stands
 * out, in the Journal where their sightings are, rather than as a banner over
 * the map. */
function UploadEntry({ capture, onReview }: { capture: CaptureSummary; onReview: () => void }) {
  const needsYou = capture.processing_state === "needs_review";
  return (
    <article className={needsYou ? "spec upload-entry needs-review" : "spec upload-entry"}
      aria-label={needsYou ? "Upload that needs you" : "Upload in progress"}
      onClick={needsYou ? onReview : undefined}>
      <div className="frame">
        {capture.thumb_url ? <img src={capture.thumb_url} alt="your upload" /> : <div className="media-placeholder">No preview yet</div>}
        {needsYou && <span className="no">NEEDS YOU</span>}
      </div>
      <div className="meta">
        {needsYou ? <>
          <div className="name">TWO ANIMALS IN ONE SHOT</div>
          <div className="line">We need you to say which photos show which animal before this goes on the map.</div>
          <button className="link-btn" onClick={(event) => { event.stopPropagation(); onReview(); }}>Separate the animals</button>
        </> : <div className="processing-status">{processingLabel(capture.processing_state)}</div>}
        <div className="line">taken {formatWhen(capture.captured_at)}</div>
      </div>
    </article>
  );
}

export default function Dex({ onUnauthorized }: { onUnauthorized: () => void }) {
  // Matches have no tab of their own: they open from inside DOGS, where the
  // dog a verdict makes or grows shows up. FLAGS and POSTS live under ADMIN.
  const [view, setView] = useState<"map" | "journal" | "dogs">("map");
  const [selectedId, setSelected] = useState<string | null>(null);
  // The map can show the whole cohort's sightings, not just the viewer's.
  // Defaults to MINE: that renders straight from the /dex data already loaded
  // for the journal, so the common case costs no extra request. /map is loaded
  // lazily; only pending media or a new upload triggers automatic refreshes.
  const [scope, setScope] = useState<"mine" | "everyone">("mine");
  const ownFeed = useProcessingFeed(getDex, view === "map" || view === "journal", onUnauthorized, false, ownBusy);
  const sharedFeed = useProcessingFeed(getMap, view === "map" && scope === "everyone", onUnauthorized);
  const captureFeed = useProcessingFeed(getCaptures, view === "map" || view === "journal", onUnauthorized, true);
  // Only uploads that are not yet sightings get a Journal entry of their own.
  // A finished one is already there as its sightings, and one with no animal
  // in it has nothing left to do.
  const pendingUploads = (captureFeed.data ?? []).filter((capture) => UNFINISHED.has(capture.processing_state));
  const [reviewCaptureId, setReviewCaptureId] = useState<string | null>(null);
  const childSignature = (captureFeed.data ?? []).flatMap((capture) => capture.sighting_ids).sort().join(",");
  useEffect(() => {
    if (childSignature) { ownFeed.refresh(); sharedFeed.refresh(); }
  }, [childSignature, ownFeed.refresh, sharedFeed.refresh]);
  const { data: sightings } = ownFeed;
  const { data: everyone, setData: setEveryone, error: everyoneError } = sharedFeed;
  const selected = sightings?.find((s) => s.id === selectedId) ?? null;
  // Set while someone is reporting a sighting from a map popup.
  const [reporting, setReporting] = useState<string | null>(null);
  // Only someone who may be posted sees the share toggle. The endpoint checks
  // consent itself -- a client-side flag is a suggestion.
  const [canShare, setCanShare] = useState(false);
  // Local overrides after a toggle, so the button answers at once instead of
  // waiting for the next /dex refresh.
  const [sharedNow, setSharedNow] = useState<Record<string, boolean>>({});
  // Whether "Add voice note" works now; null until /me answers, and then only
  // offered when it does (#156).
  const [laterNotes, setLaterNotes] = useState<LaterNotesConfig | null>(null);

  useEffect(() => {
    // Failure is not surfaced: lacking consent and the call failing look the
    // same from here, and both mean "do not offer the share toggle".
    getMe()
      .then((me) => {
        setCanShare(me.can_share_socially === true);
        setLaterNotes({ enabled: me.later_voice_notes_enabled === true, max: me.later_voice_notes_max ?? 0 });
      })
      .catch(() => {});
  }, []);

  async function toggleShared(id: string, shared: boolean) {
    try {
      const result = await setSightingShared(id, shared);
      setSharedNow((cur) => ({ ...cur, [id]: result.shared }));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
    }
  }

  // Catalog numbers are assigned in the order sightings were first logged —
  // a life-list. Displayed newest-first.
  const { shown, numberOf } = useMemo(() => {
    const list = sightings ?? [];
    const asc = [...list].sort(
      (a, b) => +new Date(a.captured_at) - +new Date(b.captured_at),
    );
    const numberOf = new Map(asc.map((s, i) => [s.id, i + 1]));
    const shown = [...asc].reverse();
    return { shown, numberOf };
  }, [sightings]);

  if (sightings === null) {
    return <div className="empty-state">{ownFeed.error ? <><p role="alert">Couldn't load your Journal.</p><button onClick={ownFeed.refresh}>Try again</button></> : "FETCHING YOUR GUIDE…"}</div>;
  }

  // MINE renders the /dex data already in hand. EVERYONE renders the cohort
  // response, whose sightings each carry `mine`, so a pin names an observer
  // only when it isn't the viewer's own.
  let mapBody;
  let showingMap = false;
  if (scope === "mine") {
    mapBody =
      sightings.length === 0 ? (
        // Reachable, and worth reaching: a brand-new tester has nothing of
        // their own yet, and pointing them at EVERYONE is far better than a
        // dead end. This used to short-circuit the whole view.
        <div className="empty-state">
          <span className="big">🐾</span>
          NO SIGHTINGS YET —<br />
          GO SPOT YOUR FIRST INDIE
          <button className="link-btn" onClick={() => setScope("everyone")}>
            or see everyone else's
          </button>
        </div>
      ) : (
        <DogMap sightings={sightings} />
      );
    showingMap = sightings.length > 0;
  } else if (everyoneError) {
    // Falls back to the viewer's own map rather than an error page: their
    // sightings are already loaded, so there is no reason to show nothing.
    mapBody = (
      <>
        <p className="hint" role="alert">{everyone ? "Couldn't refresh the shared map — showing the last update." : "Couldn't load the shared map — showing yours."} <button onClick={sharedFeed.refresh}>Try again</button></p>
        <DogMap sightings={everyone ?? sightings} onReport={everyone ? setReporting : undefined} />
      </>
    );
    showingMap = true;
  } else if (everyone === null) {
    mapBody = <div className="empty-state">LOADING EVERYONE'S SIGHTINGS…</div>;
  } else if (everyone.length === 0) {
    mapBody = <div className="empty-state">NO SIGHTINGS ANYWHERE YET</div>;
  } else {
    mapBody = <DogMap sightings={everyone} onReport={setReporting} />;
    showingMap = true;
  }

  return (
    // The map-view layout applies whenever a map is actually on screen, which
    // now includes a viewer with no sightings of their own looking at EVERYONE.
    <div className={view === "map" && showingMap ? "dex dex-map-view" : "dex"}>
      <div className="dex-toggle">
        <button className={view === "map" ? "active" : ""} onClick={() => setView("map")}>
          MAP
        </button>
        <button className={view === "journal" ? "active" : ""} onClick={() => setView("journal")}>
          JOURNAL
        </button>
        <button className={view === "dogs" ? "active" : ""} onClick={() => setView("dogs")}>
          DOGS
        </button>
      </div>

      {reviewCaptureId && <CaptureReview captureId={reviewCaptureId} laterNotes={laterNotesOffered(laterNotes) ? laterNotes : undefined} onClose={() => setReviewCaptureId(null)} onUnauthorized={onUnauthorized} onSaved={() => { setReviewCaptureId(null); captureFeed.refresh(); ownFeed.refresh(); sharedFeed.refresh(); }} />}

      {ownFeed.error && <p className="hint" role="alert">Couldn't refresh your Journal — showing the last update. <button onClick={ownFeed.refresh}>Try again</button></p>}
      {(view === "journal" || view === "map") && (ownFeed.paused || (scope === "everyone" && sharedFeed.paused)) && (
        <p className="hint">Still processing. Automatic updates paused. <button onClick={() => { ownFeed.refresh(); sharedFeed.refresh(); }}>Check again</button></p>
      )}

      {view === "dogs" ? (
        <Dogs onUnauthorized={onUnauthorized} />
      ) : view === "map" ? (
        <>
          <div className="scope-toggle">
            <button
              className={scope === "mine" ? "active" : ""}
              onClick={() => setScope("mine")}
            >
              MINE
            </button>
            <button
              className={scope === "everyone" ? "active" : ""}
              onClick={() => setScope("everyone")}
            >
              EVERYONE
            </button>
          </div>
          {mapBody}
        </>
      ) : sightings.length === 0 && pendingUploads.length === 0 ? (
        <div className="empty-state">
          <span className="big">🐾</span>
          NO SIGHTINGS YET —<br />
          GO SPOT YOUR FIRST INDIE
        </div>
      ) : (
        <div className="journal">
          <div className="journal-head">
            YOUR GUIDE · {sightings.length} SIGHTING{sightings.length === 1 ? "" : "S"}
          </div>
          {captureFeed.error && <p className="hint" role="alert">Couldn't load upload progress. <button onClick={captureFeed.refresh}>Try again</button></p>}
          {captureFeed.paused && <p className="hint">Upload processing is still pending. <button onClick={captureFeed.refresh}>Check uploads again</button></p>}
          {pendingUploads.map((capture) => <UploadEntry key={capture.capture_id} capture={capture} onReview={() => setReviewCaptureId(capture.capture_id)} />)}
          {shown.map((s) => (
            <div key={s.id} className="spec" onClick={() => setSelected(s.id)}>
              <div className="frame">
                {s.photos[0] ? <img src={s.photos[0].thumb_url} alt={`${speciesNoun(s.species)} sighting`} /> : <div className="media-placeholder">No preview yet</div>}
                <span className="no">No. {String(numberOf.get(s.id) ?? 0).padStart(3, "0")}</span>
              </div>
              <div className="meta">
                <div className="name anon">— UNIDENTIFIED —</div>
                {s.capture_id && s.details_editable && <button className="link-btn" onClick={(event) => { event.stopPropagation(); setReviewCaptureId(s.capture_id!); }}>Edit animal details</button>}
                {canShare && (() => {
                  const shared = sharedNow[s.id] ?? s.shared_with_team === true;
                  return (
                    <button
                      className={shared ? "link-btn share-on" : "link-btn"}
                      aria-pressed={shared}
                      onClick={(event) => { event.stopPropagation(); void toggleShared(s.id, !shared); }}
                    >
                      {shared ? "Shared with the team" : "Share with the team"}
                    </button>
                  );
                })()}
                {processingLabel(s.processing_state) && <div className="processing-status">{processingLabel(s.processing_state)}</div>}
                {s.off_map_reason && (
                  // Yours stays in your dex whatever its status. Being told is
                  // the point: otherwise it is simply missing from the shared
                  // map with no explanation anywhere.
                  <div className="under-review">
                    {s.off_map_reason === "reported"
                      ? "REPORTED · UNDER REVIEW"
                      : s.off_map_reason === "hidden"
                        ? "HIDDEN BY A MODERATOR"
                        : "NOT ON THE SHARED MAP · NO ANIMAL DETECTED"}
                  </div>
                )}
                <div className="line">
                  spotted {formatWhen(s.captured_at)}
                  <br />
                  {where(s)}
                </div>
                {attrTags(s).length > 0 && (
                  <div className="marks">
                    {attrTags(s).map((t) => (
                      <span key={t} className="mk">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {reporting && (
        <ReportSheet
          sightingId={reporting}
          species={[...(everyone ?? []), ...sightings].find((s) => s.id === reporting)?.species}
          onClose={() => setReporting(null)}
          onReported={() => {
            setReporting(null);
            // Drop it from the loaded cohort map immediately. It is hidden
            // server-side now, and leaving the pin up until a refetch says
            // the button did nothing.
            setEveryone((cur) => (cur ?? []).filter((x) => x.id !== reporting));
          }}
          onUnauthorized={onUnauthorized}
        />
      )}

      {selected && (
        <div className="viewer-overlay" onClick={() => setSelected(null)}>
          <div onClick={(e) => e.stopPropagation()}>
            {selected.photos.length > 0 ? selected.photos.map((photo, index) => <img key={photo.url} src={photo.url} alt={`${speciesNoun(selected.species)} sighting ${index + 1}`} />) : <div className="media-placeholder">{processingLabel(selected.processing_state) ?? "Preview unavailable"}</div>}
            {selected.photos.length > 0 && processingLabel(selected.processing_state) && <p className="viewer-caption">{processingLabel(selected.processing_state)}</p>}
            <p className="viewer-caption">
              No. {String(numberOf.get(selected.id) ?? 0).padStart(3, "0")} ·{" "}
              {formatWhen(selected.captured_at)}
            </p>
            {selected.attrs?.note && <p className="viewer-note">{selected.attrs.note}</p>}
            {selected.audio_url && <audio className="viewer-audio" src={selected.audio_url} controls preload="none" aria-label="voice note" />}
            <SuggestionFlow key={selected.id} species={selected.species} ids={[...(selected.suggestions ?? []), ...(selected.review_suggestions ?? [])].map((s) => s.id)}
              usedIds={(selected.answered_suggestions ?? []).filter(isUsed).map((s) => s.id)}>
              <Transcript transcripts={selected.voice_transcripts ?? []} notes={selected.later_notes ?? []} highlights={[
                ...[...(selected.suggestions ?? []), ...(selected.review_suggestions ?? [])]
                  .map((s) => ({ id: s.id, quote: s.quote, label: `${SUGGESTION_NAMES[s.field]}: ${s.value}` })),
                // Answered and saved: still marked, quieter. A rejected or corrected one was wrong, so it isn't.
                ...(selected.answered_suggestions ?? []).filter(isUsed)
                  .map((s) => ({ id: s.id, quote: s.quote, label: `${SUGGESTION_NAMES[s.field]}: ${s.value}`, used: true })),
              ]} />
              {!!selected.review_suggestions?.length && selected.capture_id && (
                <button className="btn" data-fx-chip={selected.review_suggestions.map((s) => s.id).join(" ")} onClick={() => { const id = selected.capture_id!; setSelected(null); setReviewCaptureId(id); }}>
                  Review {selected.review_suggestions.length} {selected.review_suggestions.length === 1 ? "suggestion" : "suggestions"} from your note
                </button>
              )}
              {selected.suggestions && selected.suggestions.length > 0 && (
                <SuggestedDetails
                  key={selected.id}
                  suggestions={selected.suggestions}
                  onDecided={ownFeed.refresh}
                  onUnauthorized={onUnauthorized}
                />
              )}
              {attrTags(selected).length > 0 && (
                <div className="viewer-tags">
                  {attrTagFields(selected).map(({ field, label }) => (
                    <span key={field} className="tag"
                      data-fx-into={(selected.answered_suggestions ?? []).filter((a) => isUsed(a) && a.field === field).map((a) => a.id).join(" ") || undefined}>
                      {label}
                    </span>
                  ))}
                </div>
              )}
            </SuggestionFlow>
            {laterNotesOffered(laterNotes) && <AddVoiceNote key={selected.id} target={{ sightingId: selected.id }} max={laterNotes.max}
              added={(selected.later_notes ?? []).filter((n) => n.sighting_id === selected.id).length}
              onAdded={ownFeed.refresh} onUnauthorized={onUnauthorized} />}
          </div>
        </div>
      )}
    </div>
  );
}
