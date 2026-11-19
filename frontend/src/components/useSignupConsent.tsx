import { useEffect, useState } from "react";
import ConsentFields from "./ConsentFields";
import { consentComplete, consentFormValues, emptyConsentAnswers, getConsentPolicy,
  parseConsentPolicy, type ConsentPolicy } from "../consentApi";

/** Keep the legal step separate from the auth transport and its proof/limits. */
export function useSignupConsent(passcode: boolean) {
  const [policy, setPolicy] = useState<ConsentPolicy | null>(null);
  const [answers, setAnswers] = useState(emptyConsentAnswers);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setError(null);
    getConsentPolicy().then((value) => { if (active) setPolicy(value); })
      .catch(() => { if (active) setError("Couldn’t load the current policies. Please try again."); });
    return () => { active = false; };
  }, [attempt]);

  useEffect(() => { setAnswers(emptyConsentAnswers()); }, [creating, passcode]);

  const required = !!policy?.enabled && (passcode || creating);
  return {
    policy,
    creating,
    setCreating,
    ready: !!policy && (!required || consentComplete(policy, answers)),
    values: {
      create_account: creating ? "true" : "false",
      ...(required && policy ? consentFormValues(policy, answers) : {}),
    },
    refresh(value: unknown) {
      setAnswers(emptyConsentAnswers());
      try { setPolicy(parseConsentPolicy(value)); }
      catch { setAttempt((n) => n + 1); }
    },
    fields: <>
      {!policy && !error && <p className="hint" role="status">Loading policies…</p>}
      {error && <p className="signin-error" role="alert">{error}{" "}
        <button type="button" className="link-btn" onClick={() => setAttempt((n) => n + 1)}>Try again</button></p>}
      {required && policy && <ConsentFields policy={policy} answers={answers} onChange={setAnswers} />}
    </>,
  };
}
