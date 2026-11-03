import type { AbuseSignals as Signals } from "../api";

export default function AbuseSignals({ data }: { data: Signals }) {
  const latest = data.hours[0];
  return <section aria-label="Abuse signals">
    <h3>RECENT ACTIVITY</h3>
    {latest && <p className="stats-note">This hour: {latest.signups} accounts created, {latest.failed_signins} failed sign-ins,
      and {latest.uploads} uploads accepted. {latest.upload_throttled} upload requests and {latest.email_throttled} email attempts hit limits.</p>}
    {latest?.signup_spike && <p role="status">Account creation is above the alert threshold.</p>}
    {latest?.signin_spike && <p role="status">Failed sign-ins are above the alert threshold.</p>}
    <p className="stats-note">Account creation includes addresses awaiting email verification. Failed sign-ins include throttled attempts.
      Signals start {new Date(data.collection_started_at).toLocaleString()}; they do not trigger moderation automatically.</p>
    <details><summary>Hourly activity</summary>
      <table className="stats-table"><thead><tr><th>HOUR</th><th>ACCOUNTS</th><th>FAILED SIGN-INS</th><th>UPLOADS</th><th>UPLOADS HELD</th></tr></thead>
        <tbody>{data.hours.map((h) => <tr key={h.hour}>
          <td>{new Date(h.hour).toLocaleString()}</td><td>{h.signups}</td><td>{h.failed_signins}</td><td>{h.uploads}</td><td>{h.upload_throttled}</td>
        </tr>)}</tbody></table>
    </details>
    <h3>UPLOAD SPIKES</h3>
    {data.upload_spikes.length ? <ul>{data.upload_spikes.map((o) => <li key={o.id}>
      {o.display_name ?? "An account"}: {o.recent} uploads in the last hour; {o.previous} in the previous hour.
    </li>)}</ul> : <p className="stats-note">No account has an upload spike.</p>}
    <p className="stats-note">Email budget: {data.email_capacity.reserved_last_24h}/{data.email_capacity.daily_budget} attempts reserved
      in the last 24 hours, {data.email_capacity.accepted_last_24h} accepted by the provider.
      {data.email_capacity.retry_at && <> Sending can resume after {new Date(data.email_capacity.retry_at).toLocaleString()}.</>}
    </p>
  </section>;
}
