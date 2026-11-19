import { API_BASE } from "./apiBase";

/**
 * Tells the API this client implements the consent prompt (#228), so it may
 * be answered 428 consent_required. Clients without it (older native builds)
 * are let through until the server's CONSENT_LEGACY_CLIENTS_UNTIL instead,
 * because they treat 428 as a permanent failure.
 *
 * Added to every request to our own API by wrapping fetch once at startup,
 * rather than at each of the ~50 call sites. Requests elsewhere (presigned
 * storage URLs, third parties) are left untouched: a custom header there
 * would add a CORS preflight or break a signature.
 */
export const CONSENT_CAPABILITY_HEADER = "X-IndieDex-Consent-Flow";

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

export function isOwnApi(url: string, origin = globalThis.location?.origin ?? ""): boolean {
  if (API_BASE) return url === API_BASE || url.startsWith(`${API_BASE}/`);
  if (url.startsWith("/") && !url.startsWith("//")) return true;
  return !!origin && (url === origin || url.startsWith(`${origin}/`));
}

export function withConsentCapability(fetchImpl: typeof fetch): typeof fetch {
  return (input, init) => {
    if (!isOwnApi(requestUrl(input))) return fetchImpl(input, init);
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    if (!headers.has(CONSENT_CAPABILITY_HEADER)) headers.set(CONSENT_CAPABILITY_HEADER, "1");
    return fetchImpl(input, { ...init, headers });
  };
}

let installed = false;

export function installConsentCapability(): void {
  if (installed || typeof globalThis.fetch !== "function") return;
  installed = true;
  globalThis.fetch = withConsentCapability(globalThis.fetch.bind(globalThis));
}
