import { useEffect, useMemo, useState } from "react";
import {
  getDex,
  getMap,
  getMe,
  type Sighting,
} from "../api";
import DogMap from "../components/DogMap";
import ReportSheet from "../components/ReportSheet";
import Dogs from "./Dogs";
import Moderation from "./Moderation";
import Review from "./Review";
import { useProcessingFeed } from "../useProcessingFeed";
import { processingLabel } from "../processing";

const TAG_LABELS: Record<string, string> = {
  male: "♂ MALE",
  female: "♀ FEMALE",
  left: "NOTCH-L",
  right: "NOTCH-R",
  healthy: "HEALTHY",
  injured: "INJURED",
};

function attrTags(s: Sighting): string[] {
  const attrs = s.attrs || {};
  const raw = [attrs.sex, attrs.ear_notch, attrs.condition] as (string | undefined)[];
  return raw
    .filter((v): v is string => !!v && v !== "unsure" && v !== "none")
    .map((v) => TAG_LABELS[v] ?? v);
}

function when(iso: string): string {
  const d = new Date(iso);
  const day = d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${day} · ${time}`;
}

function where(s: Sighting): string {
  if (s.lat != null && s.lng != null) return `${s.lat.toFixed(3)}, ${s.lng.toFixed(3)}`;
  return "no GPS";
}

export default function Dex({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [view, setView] = useState<"map" | "journal" | "dogs" | "review" | "flags">("map");
  const [selectedId, setSelected] = useState<string | null>(null);
  // The map can show the whole cohort's sightings, not just the viewer's.
  // Defaults to MINE: that renders straight from the /dex data already loaded
  // for the journal, so the common case costs no extra request. /map is loaded
  // lazily; only pending media or a new upload triggers automatic refreshes.
  const [scope, setScope] = useState<"mine" | "everyone">("mine");
  const ownFeed = useProcessingFeed(getDex, view === "map" || view === "journal", onUnauthorized);
  const sharedFeed = useProcessingFeed(getMap, view === "map" && scope === "everyone", onUnauthorized);
  const { data: sightings } = ownFeed;
  const { data: everyone, setData: setEveryone, error: everyoneError } = sharedFeed;
  const selected = sightings?.find((s) => s.id === selectedId) ?? null;
  // Set while someone is reporting a sighting from a map popup.
  const [reporting, setReporting] = useState<string | null>(null);
  // Only decides whether the FLAGS tab renders. The endpoints behind it check
  // the tier themselves -- a client-side flag is a suggestion.
  const [isModerator, setIsModerator] = useState(false);

  useEffect(() => {
    // Failure is not surfaced: not being a moderator and the call failing look
    // the same from here, and both mean "do not show the tab".
    getMe()
      .then((me) => setIsModerator(me.is_moderator))
      .catch(() => {});
  }, []);

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
        <button className={view === "review" ? "active" : ""} onClick={() => setView("review")}>
          MATCHES
        </button>
        {isModerator && (
          <button className={view === "flags" ? "active" : ""} onClick={() => setView("flags")}>
            FLAGS
          </button>
        )}
      </div>

      {ownFeed.error && <p className="hint" role="alert">Couldn't refresh your Journal — showing the last update. <button onClick={ownFeed.refresh}>Try again</button></p>}
      {(view === "journal" || view === "map") && (ownFeed.paused || (scope === "everyone" && sharedFeed.paused)) && (
        <p className="hint">Still processing. Automatic updates paused. <button onClick={() => { ownFeed.refresh(); sharedFeed.refresh(); }}>Check again</button></p>
      )}

      {view === "flags" ? (
        <Moderation onUnauthorized={onUnauthorized} />
      ) : view === "review" ? (
        <Review onUnauthorized={onUnauthorized} />
      ) : view === "dogs" ? (
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
      ) : sightings.length === 0 ? (
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
          {shown.map((s) => (
            <div key={s.id} className="spec" onClick={() => setSelected(s.id)}>
              <div className="frame">
                {s.photos[0] ? <img src={s.photos[0].thumb_url} alt="dog sighting" /> : <div className="media-placeholder">No preview yet</div>}
                <span className="no">No. {String(numberOf.get(s.id) ?? 0).padStart(3, "0")}</span>
              </div>
              <div className="meta">
                <div className="name anon">— UNIDENTIFIED —</div>
                {processingLabel(s.processing_state) && <div className="processing-status">{processingLabel(s.processing_state)}</div>}
                {s.review_status && s.review_status !== "valid" && (
                  // Yours stays in your dex whatever its status. Being told is
                  // the point: otherwise it is simply missing from the shared
                  // map with no explanation anywhere.
                  <div className="under-review">
                    {s.review_status === "pending"
                      ? "REPORTED · UNDER REVIEW"
                      : "HIDDEN BY A MODERATOR"}
                  </div>
                )}
                <div className="line">
                  spotted {when(s.captured_at)}
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
            {selected.photos.length > 0 ? selected.photos.map((photo, index) => <img key={photo.url} src={photo.url} alt={`dog sighting ${index + 1}`} />) : <div className="media-placeholder">{processingLabel(selected.processing_state) ?? "Preview unavailable"}</div>}
            {selected.photos.length > 0 && processingLabel(selected.processing_state) && <p className="viewer-caption">{processingLabel(selected.processing_state)}</p>}
            <p className="viewer-caption">
              No. {String(numberOf.get(selected.id) ?? 0).padStart(3, "0")} ·{" "}
              {when(selected.captured_at)}
            </p>
            {selected.attrs?.note && <p className="viewer-note">{selected.attrs.note}</p>}
            {attrTags(selected).length > 0 && (
              <div className="viewer-tags">
                {attrTags(selected).map((t) => (
                  <span key={t} className="tag">
                    {t}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
