import { useEffect, useState } from "react";
import Capture from "./screens/Capture";
import Dex from "./screens/Dex";
import SignIn from "./screens/SignIn";
import { failedCount, flush, setOnFlushed, setOnUnauthorized } from "./offline/queue";
import FailedSightings from "./components/FailedSightings";
import { getDex, UnauthorizedError } from "./api";
import { API_BASE } from "./apiBase";
import { listenForAuthLinks } from "./deepLink";
import mark from "./assets/mark.svg";

type Tab = "capture" | "dex";

export default function App() {
  const [tab, setTab] = useState<Tab>("capture");
  const [unauthorized, setUnauthorized] = useState(false);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [failed, setFailed] = useState(0);
  const [showFailed, setShowFailed] = useState(false);

  useEffect(() => {
    async function run() {
      await flush();
      setFailed(await failedCount());
    }
    run();
    window.addEventListener("online", run);
    // A backgrounded-then-resumed PWA doesn't remount, so mount-only misses
    // "app open" in the PWA sense -- catch it on foreground resume too.
    function onVisibilityChange() {
      if (document.visibilityState === "visible") run();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    // flush() can also be triggered elsewhere (Capture's submit()) without
    // App knowing when it resolves -- have flush() itself notify us so the
    // badge count stays current mid-session, not just on mount/online.
    setOnFlushed(() => {
      failedCount().then(setFailed);
    });
    // A 401 during a background flush means the session is dead -- reuse the
    // same invite/login gate the initial getDex() auth probe shows, rather
    // than leaving the user stuck with a growing unsyncable badge.
    setOnUnauthorized(() => setUnauthorized(true));
    return () => {
      window.removeEventListener("online", run);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      setOnFlushed(null);
      setOnUnauthorized(null);
    };
  }, []);

  // One-time auth probe so the invite-needed state shows immediately on
  // load, rather than only after the user tries to submit or view the dex.
  function reprobeAuth() {
    setCheckingAuth(true);
    getDex()
      .then(() => {
        setUnauthorized(false);
        setCheckingAuth(false);
      })
      .catch((err) => {
        if (err instanceof UnauthorizedError) setUnauthorized(true);
        setCheckingAuth(false);
      });
  }

  useEffect(() => {
    reprobeAuth();
  }, []);

  // A magic-link email opens directly in the app (Universal Links) instead
  // of Safari -- the OS hands us the URL with nothing consumed yet.
  useEffect(() => {
    listenForAuthLinks(reprobeAuth);
  }, []);

  if (checkingAuth) {
    return <div className="app" />;
  }

  if (unauthorized) {
    return (
      <div className="app">
        {/* Re-probe rather than trusting the POST: the server just set a
            session cookie, and getDex() succeeding is the only proof the
            app and server agree about who we are. */}
        <SignIn onSignedIn={reprobeAuth} />
      </div>
    );
  }

  return (
    <div className="app">
      <div className="topbar">
        <span className="brand">
          <img className="mark" src={mark} alt="" />
          indiedex
        </span>
        {failed > 0 && (
          <button className="failed-badge" onClick={() => setShowFailed(true)}>
            {failed} couldn't sync
          </button>
        )}
        <button
          className="signout"
          onClick={async () => {
            // Confirm first: the tabbar is right there and a stray tap costs
            // a round-trip through email to get back in.
            if (!confirm("Sign out of indiedex?")) return;
            await fetch(`${API_BASE}/auth/logout`, {
              method: "POST",
              headers: { Accept: "application/json" },
            }).catch(() => {});
            // Show the gate regardless. If the request failed the cookie may
            // survive, but the next auth probe settles it either way.
            setUnauthorized(true);
          }}
        >
          sign out
        </button>
      </div>
      <div className="screen">
        {tab === "capture" ? (
          <Capture />
        ) : (
          <Dex onUnauthorized={() => setUnauthorized(true)} />
        )}
      </div>
      <div className="tabbar">
        <button className={tab === "capture" ? "active" : ""} onClick={() => setTab("capture")}>
          <span className="icon">📷</span>
          SPOT
        </button>
        <button className={tab === "dex" ? "active" : ""} onClick={() => setTab("dex")}>
          <span className="icon">📖</span>
          INDIEDEX
        </button>
      </div>
      {showFailed && (
        <FailedSightings
          onClose={() => {
            setShowFailed(false);
            failedCount().then(setFailed);
          }}
        />
      )}
    </div>
  );
}
