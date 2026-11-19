import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../apiBase";
import { UnauthorizedError } from "../api";
import { acceptAccountConsent, consentComplete, emptyConsentAnswers, getAccountConsent,
  isConsentRequired, type ConsentPolicy } from "../consentApi";
import ConsentFields from "./ConsentFields";

/** A native modal preserves the capture/draft beneath it while blocking use. */
export default function ConsentReview({ onAccepted, onSignOut, onDeleteAccount, onUnauthorized }: {
  onAccepted: () => void;
  onSignOut: () => void;
  onDeleteAccount: () => void;
  onUnauthorized: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [policy, setPolicy] = useState<ConsentPolicy | null>(null);
  const [answers, setAnswers] = useState(emptyConsentAnswers);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      const status = await getAccountConsent();
      if (!status.required) { onAccepted(); return; }
      setPolicy(status.policy);
      setAnswers(emptyConsentAnswers());
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setError("Couldn’t load the current policies. Please try again.");
    }
  }

  useEffect(() => {
    const element = dialog.current;
    if (element?.showModal) element.showModal();
    else element?.setAttribute("open", "");
    void load();
    return () => { if (element?.close) element.close(); };
    // Only on opening: unrelated queue or account updates must not reset answers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!policy || !consentComplete(policy, answers)) return;
    setBusy(true);
    setError(null);
    try {
      const status = await acceptAccountConsent(policy, answers);
      if (!status.required) onAccepted();
      else {
        setPolicy(status.policy);
        setAnswers(emptyConsentAnswers());
        setError("The policies changed. Please review the current versions and confirm again.");
      }
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else if (isConsentRequired(err)) {
        await load();
        setError("The policies changed. Please review the current versions and confirm again.");
      } else setError("Couldn’t save your agreement. Please try again.");
    } finally { setBusy(false); }
  }

  return (
    <dialog ref={dialog} className="consent-dialog" aria-labelledby="consent-title" onCancel={(event) => event.preventDefault()}>
      <h2 id="consent-title">Please review our policies</h2>
      <p>Before continuing, agree to our current terms and guidelines and acknowledge our privacy policy.</p>
      {error && <p className="signin-error" role="alert">{error}</p>}
      {policy ? (
        <form onSubmit={submit}>
          <ConsentFields policy={policy} answers={answers} onChange={setAnswers} disabled={busy} legend="Your agreement" />
          <button className="btn btn-primary" type="submit" disabled={busy || !consentComplete(policy, answers)}>
            {busy ? "Saving…" : "Agree and continue"}
          </button>
        </form>
      ) : <button type="button" className="link-btn" onClick={() => void load()}>Load policies</button>}
      <nav className="consent-help" aria-label="Policies and support">
        <a href={`${API_BASE}/policies/terms`} target="_blank" rel="noreferrer">Terms of Service</a>
        <a href={`${API_BASE}/policies/community-guidelines`} target="_blank" rel="noreferrer">Community Guidelines</a>
        <a href={`${API_BASE}/policies/privacy`} target="_blank" rel="noreferrer">Privacy Policy</a>
        <a href={`${API_BASE}/policies/grievance`} target="_blank" rel="noreferrer">Grievances</a>
        <a href={`${API_BASE}/support`} target="_blank" rel="noreferrer">Support</a>
      </nav>
      <div className="consent-help">
        <button type="button" className="link-btn" onClick={onDeleteAccount}>Delete account</button>
        <button type="button" className="link-btn" onClick={onSignOut}>Sign out</button>
      </div>
    </dialog>
  );
}
