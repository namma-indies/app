import { useEffect, useState } from "react";
import {
  getStats,
  getStatsAreas,
  getStatsObservers,
  UnauthorizedError,
  type Stats as StatsPayload,
  type StatsAreas,
  type StatsObserver,
} from "../api";

/** The operator's view of the corpus: how many people have seen how many dogs.
 *
 * Moderator-gated, and every endpoint behind it checks the tier itself -- the
 * tab only renders for a moderator, but that is a display hint, not the
 * control.
 *
 * The empty state carries most of the weight here and is written deliberately.
 * With today's data the area table is empty, and a bare "no data" would be a
 * lie: there are 71 sightings, they are simply outside Bangalore's wards or in
 * wards too thin to report. `areas_suppressed` and `unattributed_sightings`
 * are what make the numbers reconcile, so they are rendered as sentences
 * rather than hidden as fine print.
 */

function short(when: string | null): string {
  if (!when) return "—";
  return new Date(when).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function Stats({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [stats, setStats] = useState<StatsPayload | null>(null);
  const [areas, setAreas] = useState<StatsAreas | null>(null);
  const [observers, setObservers] = useState<StatsObserver[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    Promise.all([getStats(), getStatsAreas(), getStatsObservers()])
      .then(([s, a, o]) => {
        setStats(s);
        setAreas(a);
        setObservers(o.observers);
      })
      .catch((e) => {
        if (e instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
  }, [onUnauthorized]);

  if (failed) return <div className="stats"><p className="stats-note">Couldn't load the numbers.</p></div>;
  if (!stats || !areas || !observers) return <div className="stats"><p className="stats-note">Counting…</p></div>;

  const peak = Math.max(1, ...stats.months.map((m) => m.sightings));

  return (
    <div className="stats">
      <div className="stat-row">
        <div className="stat">
          <b>{stats.totals.observers}</b>
          <span>PEOPLE</span>
        </div>
        <div className="stat">
          <b>{stats.totals.sightings}</b>
          <span>SIGHTINGS</span>
        </div>
        <div className="stat">
          <b>{stats.totals.confirmed_individuals}</b>
          <span>DOGS NAMED AS ONE</span>
        </div>
      </div>
      {/* Sightings overcount dogs, confirmed individuals undercount them. Saying
          so beats printing one number and letting a reader assume it is exact. */}
      <p className="stats-note">
        A dog seen ten times is ten sightings. <b>{stats.totals.confirmed_individuals}</b> is
        only the ones someone has confirmed as the same animal twice — the true
        number of dogs is somewhere between.
      </p>

      <h3>BY MONTH</h3>
      <div className="months">
        {stats.months.map((m) => (
          <div className="month" key={m.month}>
            <div className="bar" style={{ height: `${(m.sightings / peak) * 100}%` }} />
            <span className="n">{m.sightings}</span>
            <span className="lbl">{m.month.slice(2)}</span>
          </div>
        ))}
      </div>

      <h3>BY AREA <span className="kind">{areas.kind.replace(/_/g, " ")}</span></h3>
      {areas.areas.length > 0 ? (
        <table className="stats-table">
          <thead>
            <tr><th>AREA</th><th>SIGHTINGS</th><th>PEOPLE</th><th>LAST</th></tr>
          </thead>
          <tbody>
            {areas.areas.map((a) => (
              <tr key={a.id}>
                <td>{a.name ?? a.ext_code ?? "—"}</td>
                <td>{a.sightings}</td>
                <td>{a.observers}</td>
                <td>{a.last_active_month ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="stats-note">No area is dense enough to show yet.</p>
      )}
      {/* The two numbers that make the area table reconcile with the total.
          Without them an empty table reads as "we have no data", which is the
          opposite of true. */}
      <p className="stats-note">
        {areas.areas_suppressed > 0 && (
          <>
            <b>{areas.areas_suppressed}</b> area
            {areas.areas_suppressed === 1 ? " has" : "s have"} sightings but too
            few to show without pointing at a particular dog.{" "}
          </>
        )}
        {areas.unattributed_sightings > 0 && (
          <>
            <b>{areas.unattributed_sightings}</b> sighting
            {areas.unattributed_sightings === 1 ? " is" : "s are"} outside every{" "}
            {areas.kind.replace(/_/g, " ")} — logged somewhere this map doesn't
            cover yet, not missing.
          </>
        )}
      </p>

      <h3>PEOPLE</h3>
      <table className="stats-table">
        <thead>
          <tr><th>WHO</th><th>SIGHTINGS</th><th>DOGS</th><th>LAST SEEN</th></tr>
        </thead>
        <tbody>
          {observers.map((o) => (
            <tr key={o.id}>
              <td>
                {o.display_name ?? "—"}
                {o.trust_tier === "moderator" && <span className="tag">MOD</span>}
                <span className="sub">{o.email ?? o.created_via ?? ""}</span>
              </td>
              <td>{o.sightings}</td>
              <td>{o.confirmed_individuals}</td>
              <td>{short(o.last_sighting_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
