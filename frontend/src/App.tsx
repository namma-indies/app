import { useEffect, useState } from "react";
import Capture from "./screens/Capture";
import Dex from "./screens/Dex";
import { failedCount, flush } from "./offline/queue";
import FailedSightings from "./components/FailedSightings";
import { getDex, UnauthorizedError } from "./api";
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
    return () => window.removeEventListener("online", run);
  }, []);

  // One-time auth probe so the invite-needed state shows immediately on
  // load, rather than only after the user tries to submit or view the dex.
  useEffect(() => {
    getDex()
      .then(() => setCheckingAuth(false))
      .catch((err) => {
        if (err instanceof UnauthorizedError) setUnauthorized(true);
        setCheckingAuth(false);
      });
  }, []);

  if (checkingAuth) {
    return <div className="app" />;
  }

  if (unauthorized) {
    return (
      <div className="app">
        <div className="screen gate">
          <div className="big-paw">🐕‍🦺</div>
          <h2>You need an invite</h2>
          <p className="hint">
            indiedex, by Namma Indies, is invite-only right now. Ask a friend for a
            magic link, or check your email for one, to start logging sightings.
          </p>
        </div>
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
      </div>
      <div className="screen">
        {tab === "capture" ? (
          <Capture onUnauthorized={() => setUnauthorized(true)} />
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
