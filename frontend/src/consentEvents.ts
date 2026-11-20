export const CONSENT_REQUIRED_EVENT = "indiedex:consent-required";
export const CONSENT_ACCEPTED_EVENT = "indiedex:consent-accepted";

export function requestConsent(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(CONSENT_REQUIRED_EVENT));
}
