export const CONSENT_REQUIRED_EVENT = "indiedex:consent-required";

export function requestConsent(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(CONSENT_REQUIRED_EVENT));
}
