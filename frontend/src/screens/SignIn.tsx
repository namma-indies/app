import { useState } from "react";
import { API_BASE } from "../apiBase";

/** The gate an unauthenticated visitor lands on.
 *
 * This used to be a dead end -- it said "ask a friend for a magic link" and
 * offered nothing to act on, so the only way in was knowing to type /join in
 * the URL bar. Both doors now live here, in the app, and posting stays put.
 *
 * The endpoints are the same ones /join uses; they answer JSON when asked,
 * so there is one implementation of the flow rather than two.
 */
/** The Namma Indies mark -- a curled tail, the same path as the site's
 *  favicon and the iOS app icon. This screen is the app's face for anyone
 *  who hasn't signed in yet, so it should carry the mark rather than a
 *  stock emoji: the guide-dog glyph that used to sit here read as a service
 *  animal in a harness, which is the opposite of an indie on the street. */
function Mark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" role="img" aria-label="Namma Indies">
      <path
        d="M9 25 C9 16 13 12 19 12 C24 12 27 15 27 19.5 C27 23.5 23.5 25.5 20.5 24 C18.3 22.9 18 20 20 18.7"
        fill="none"
        stroke="currentColor"
        strokeWidth="3.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [mode, setMode] = useState<"email" | "passcode">("email");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [passcode, setPasscode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);

  async function post(path: string, body: Record<string, string>) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
        body: new URLSearchParams(body),
      });
      // A non-JSON reply means something upstream ate the request (offline,
      // a proxy error page). Say so rather than throwing a parse error.
      const data = await res.json().catch(() => null);
      if (!res.ok || !data?.ok) {
        setError(data?.error ?? "Something went wrong. Try again in a moment.");
        return null;
      }
      return data;
    } catch {
      setError("Can't reach the server. Check your connection and try again.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function submitEmail(e: React.FormEvent) {
    e.preventDefault();
    const data = await post("/auth/email", { email });
    if (data) setSent(data.message);
  }

  async function submitPasscode(e: React.FormEvent) {
    e.preventDefault();
    const data = await post("/auth/join", { name, passcode });
    // The session cookie is already set on that response; re-probe rather
    // than assuming, so the app and the server agree on who we are.
    if (data) onSignedIn();
  }

  if (sent) {
    return (
      <div className="screen gate">
        <div className="big-paw">📬</div>
        <h2>Check your email</h2>
        <p className="hint">{sent}</p>
        <p className="hint">
          The link works once and expires in 30 minutes. Open it on this phone
          so you land back here signed in.
        </p>
        <button className="btn btn-secondary" onClick={() => { setSent(null); setEmail(""); }}>
          Use a different address
        </button>
      </div>
    );
  }

  return (
    <div className="screen gate">
      <Mark />
      <h2>Sign in to start</h2>
      <p className="hint">
        indiedex, by Namma Indies. Log the street dogs you meet; we'll work out
        who's who. Early days — anyone's welcome to join.
      </p>

      {error && <p className="signin-error" role="alert">{error}</p>}

      {mode === "email" ? (
        <>
          <form className="signin-form" onSubmit={submitEmail}>
            <label htmlFor="signin-email">Email</label>
            <input
              id="signin-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              required
              disabled={busy}
            />
            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? "Sending…" : "Email me a link"}
            </button>
          </form>
          <div className="signin-or">or</div>
          <button className="btn btn-secondary" onClick={() => { setMode("passcode"); setError(null); }}>
            Use a passcode
          </button>
        </>
      ) : (
        <>
          <form className="signin-form" onSubmit={submitPasscode}>
            <label htmlFor="signin-name">Your name</label>
            <input
              id="signin-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="your email works best"
              autoComplete="name"
              required
              disabled={busy}
            />
            <label htmlFor="signin-passcode">Passcode</label>
            <input
              id="signin-passcode"
              type="password"
              value={passcode}
              onChange={(e) => setPasscode(e.target.value)}
              placeholder="shared code"
              autoComplete="off"
              required
              disabled={busy}
            />
            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? "Joining…" : "Join with passcode"}
            </button>
          </form>
          <p className="hint">
            Using your email as your name means your sightings follow you when
            you later sign in properly.
          </p>
          <div className="signin-or">or</div>
          <button className="btn btn-secondary" onClick={() => { setMode("email"); setError(null); }}>
            Email me a link instead
          </button>
        </>
      )}

      {/* The gate is where an app reviewer lands before they have credentials,
          so the policy and a way to reach us have to be readable from here. */}
      <div className="legal-row">
        <a href="https://nammaindies.org/privacy" target="_blank" rel="noreferrer">
          privacy
        </a>
        <span aria-hidden="true"> · </span>
        <a href="mailto:nammaindies@gmail.com">contact us</a>
      </div>
    </div>
  );
}
