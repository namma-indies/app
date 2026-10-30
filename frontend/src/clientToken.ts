/** A stable identifier for one capture, or one voice note added later.
 *
 * Minted once (at enqueue, or when a note is recorded) and never regenerated
 * -- that is the whole point. Every attempt resends the same value, so a
 * server that already stored it recognises the repeat instead of creating a
 * second row.
 *
 * The failure it closes: `flush` posts, waits, and only deletes the queued item
 * once the response arrives. When the request lands but the *response* is lost
 * -- signal dropped mid-upload, phone asleep, tab closed -- the item stays
 * pending and the next pass posts it again. That is ordinary on mobile data,
 * which is what this app runs on.
 *
 * randomUUID needs a secure context, which the app always has (it also needs
 * one for the camera and geolocation). The fallback keeps a dev server on plain
 * http working rather than throwing.
 */
export function newClientToken(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random()
    .toString(36)
    .slice(2)}`;
}
