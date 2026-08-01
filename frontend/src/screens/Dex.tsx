import { useEffect, useMemo, useState } from "react";
import { getDex, UnauthorizedError, type Sighting } from "../api";
import DogMap from "../components/DogMap";

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
  const [sightings, setSightings] = useState<Sighting[] | null>(null);
  const [view, setView] = useState<"map" | "journal">("map");
  const [selected, setSelected] = useState<Sighting | null>(null);

  useEffect(() => {
    getDex()
      .then((res) => setSightings(res.sightings))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setSightings([]);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    return <div className="empty-state">FETCHING YOUR GUIDE…</div>;
  }

  return (
    <div className={view === "map" && sightings.length > 0 ? "dex dex-map-view" : "dex"}>
      <div className="dex-toggle">
        <button className={view === "map" ? "active" : ""} onClick={() => setView("map")}>
          MAP
        </button>
        <button className={view === "journal" ? "active" : ""} onClick={() => setView("journal")}>
          JOURNAL
        </button>
      </div>

      {sightings.length === 0 ? (
        <div className="empty-state">
          <span className="big">🐾</span>
          NO SIGHTINGS YET —<br />
          GO SPOT YOUR FIRST INDIE
        </div>
      ) : view === "map" ? (
        <DogMap sightings={sightings} />
      ) : (
        <div className="journal">
          <div className="journal-head">
            YOUR GUIDE · {sightings.length} SIGHTING{sightings.length === 1 ? "" : "S"}
          </div>
          {shown.map((s) => (
            <div key={s.id} className="spec" onClick={() => setSelected(s)}>
              <div className="frame">
                {s.photos[0] && <img src={s.photos[0].thumb_url} alt="dog sighting" />}
                <span className="no">No. {String(numberOf.get(s.id) ?? 0).padStart(3, "0")}</span>
              </div>
              <div className="meta">
                <div className="name anon">— UNIDENTIFIED —</div>
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

      {selected && (
        <div className="viewer-overlay" onClick={() => setSelected(null)}>
          <div onClick={(e) => e.stopPropagation()}>
            {selected.photos[0] && <img src={selected.photos[0].url} alt="dog sighting" />}
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
