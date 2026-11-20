/**
 * Something changed who this person can see (#180) -- an unblock, from a
 * sheet far from the map. Screens holding the shared map look again.
 */
type Listener = () => void;
const listeners = new Set<Listener>();

export function onVisibilityChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function visibilityChanged(): void {
  for (const listener of [...listeners]) listener();
}
