import { useState } from "react";

/** The gate an unauthenticated visitor lands on.
 *
 * This used to be a dead end -- it said "ask a friend for a magic link" and
 * offered nothing to act on, so the only way in was knowing to type /join in
 * the URL bar. Both doors now live here, in the app, and posting stays put.
 *
 * The endpoints are the same ones /join uses; they answer JSON when asked,
 * so there is one implementation of the flow rather than two.
 */
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
      const res = await fetch(path, {
        method: "POST",
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
      <div className="big-paw">🐕‍🦺</div>
      <h2>Sign in to start</h2>
      <p className="hint">
        indiedex, by Namma Indies, is invite-only while we pilot it.
      </p>

      {error && <p className="signin-error" role="alert">{error}</p>}

      {mode === "email" ? (
        <>
          <form className="signin-form" onSubmit={submitEmail}>
            <label htmlFor="signin-email">Work email</label>
            <input
              id="signin-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@dognosis.tech"
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
              placeholder="your work email works best"
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
            Using your work email as your name means your sightings follow you
            when you later sign in properly.
          </p>
          <div className="signin-or">or</div>
          <button className="btn btn-secondary" onClick={() => { setMode("email"); setError(null); }}>
            Email me a link instead
          </button>
        </>
      )}
    </div>
  );
}
