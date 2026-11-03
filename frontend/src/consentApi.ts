import { API_BASE } from "./apiBase";
import { HttpError, UnauthorizedError, responseError } from "./api";

export interface ConsentPolicy {
  enabled: boolean;
  terms_version: string;
  privacy_version: string;
  community_guidelines_version: string;
  minimum_age: number | null;
}

export interface ConsentAnswers {
  agree_terms_and_guidelines: boolean;
  acknowledge_privacy: boolean;
  age_confirmed: boolean;
}

export const emptyConsentAnswers = (): ConsentAnswers => ({
  agree_terms_and_guidelines: false, acknowledge_privacy: false, age_confirmed: false,
});

export function consentComplete(policy: ConsentPolicy, answers: ConsentAnswers): boolean {
  return answers.agree_terms_and_guidelines && answers.acknowledge_privacy
    && (policy.minimum_age === null || answers.age_confirmed);
}

export function parseConsentPolicy(value: unknown): ConsentPolicy {
  const policy = value as Partial<ConsentPolicy> | null;
  if (!policy || typeof policy.enabled !== "boolean"
      || typeof policy.terms_version !== "string" || typeof policy.privacy_version !== "string"
      || typeof policy.community_guidelines_version !== "string"
      || !(policy.minimum_age === null || (Number.isInteger(policy.minimum_age) && Number(policy.minimum_age) > 0))) {
    throw new Error("Couldn’t load the current policies. Please try again.");
  }
  if (policy.enabled && (!policy.terms_version || !policy.privacy_version || !policy.community_guidelines_version)) {
    throw new Error("The current policies are not ready. Please try again later.");
  }
  return policy as ConsentPolicy;
}

export function consentPayload(policy: ConsentPolicy, answers: ConsentAnswers) {
  return {
    terms_version: policy.terms_version, privacy_version: policy.privacy_version,
    community_guidelines_version: policy.community_guidelines_version,
    minimum_age: policy.minimum_age, ...answers,
  };
}

export function consentFormValues(policy: ConsentPolicy, answers: ConsentAnswers): Record<string, string> {
  if (!policy.enabled) return {};
  return Object.fromEntries(Object.entries(consentPayload(policy, answers))
    .filter(([, value]) => value !== null).map(([key, value]) => [key, String(value)]));
}

async function readResponse(res: Response): Promise<unknown> {
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    const error = responseError(res);
    const body = await res.json().catch(() => null);
    if (typeof body?.detail === "string") error.message = body.detail;
    throw error;
  }
  return res.json();
}

export async function getConsentPolicy(): Promise<ConsentPolicy> {
  return parseConsentPolicy(await readResponse(await fetch(`${API_BASE}/auth/consent`, {
    credentials: "include", cache: "no-store",
  })));
}

export interface AccountConsent {
  policy: ConsentPolicy;
  required: boolean;
  accepted_at: string | null;
}

function parseAccountConsent(value: unknown): AccountConsent {
  const status = value as Partial<AccountConsent> | null;
  if (!status || typeof status.required !== "boolean") throw new Error("Couldn’t check your policy agreement.");
  return { policy: parseConsentPolicy(status.policy), required: status.required, accepted_at: status.accepted_at ?? null };
}

export async function getAccountConsent(): Promise<AccountConsent> {
  return parseAccountConsent(await readResponse(await fetch(`${API_BASE}/account/consent`, {
    credentials: "include", cache: "no-store",
  })));
}

export async function acceptAccountConsent(policy: ConsentPolicy, answers: ConsentAnswers): Promise<AccountConsent> {
  return parseAccountConsent(await readResponse(await fetch(`${API_BASE}/account/consent`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(consentPayload(policy, answers)),
  })));
}

export const isConsentRequired = (error: unknown): boolean => error instanceof HttpError && error.status === 428;
