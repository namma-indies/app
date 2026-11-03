import { useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";
import Capture from "./screens/Capture";
import Dex from "./screens/Dex";
import Admin from "./screens/Admin";
import SignIn from "./screens/SignIn";
import { failedCount, flush, setOnFlushed, setOnUnauthorized } from "./offline/queue";
import Celebration from "./components/Celebration";
import FailedSightings from "./components/FailedSightings";
import HapticsCheck, { HAPTICS_CHECK_PARAM, wantsHapticsCheck } from "./components/HapticsCheck";
import { getAccount, getDex, getMe, isAccountRestricted, UnauthorizedError, type Account } from "./api";
import DeleteAccount from "./components/DeleteAccount";
import BlockedPeople from "./components/BlockedPeople";
import { visibilityChanged } from "./visibility";
import PendingDeletion from "./screens/PendingDeletion";
import { forgetNoteLanguage } from "./components/NoteLanguagePicker";
import { API_BASE } from "./apiBase";
import { listenForAuthLinks } from "./deepLink";
import mark from "./assets/mark.svg";

type Tab = "capture" | "dex" | "admin";

/** How long the brand is held in the app to open the hidden Haptics check. */
export const HAPTICS_HOLD_MS = 2000;

export default function App() {
  const [tab, setTab] = useState<Tab>(() =>
    new URLSearchParams(window.location.search).get("tab") === "moderation" ? "admin" : "capture");
  // Display hint only: /stats/observers checks the tier itself and 404s
  // without it, so a forged flag here reveals nothing.
  const [isModerator, setIsModerator] = useState(false);
  const [unauthorized, setUnauthorized] = useState(false);
  const [checkingAuth, setCheckingAuth] = useState(true);
  // Set while the account is scheduled for deletion (#179): the app is
  // replaced by when it goes and the way to keep it.
  const [pendingDeletion, setPendingDeletion] = useState<Account | null>(null);
  const [deletingAccount, setDeletingAccount] = useState(false);
  // Set when a moderator has banned this account (#180).
  const [suspended, setSuspended] = useState(false);
  const [showingBlocks, setShowingBlocks] = useState(false);
  const [failed, setFailed] = useState(0);
  const [showFailed, setShowFailed] = useState(false);
  const [hapticsCheck, setHapticsCheck] = useState(() => wantsHapticsCheck(window.location.search));
  const brandHold = useRef<number>();

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
        setPendingDeletion(null);
        setSuspended(false);
        setCheckingAuth(false);
        // Piggybacks on the existing probe rather than adding a second
        // round trip on every load. A failure here is not an auth problem --
        // it just means no extra tab.
        getMe()
          .then((me) => setIsModerator(me.is_moderator))
          .catch(() => setIsModerator(false));
      })
      .catch(async (err) => {
        if (err instanceof UnauthorizedError) setUnauthorized(true);
        else if (isAccountRestricted(err)) {
          // Everything but /account answers 403 while deletion is pending, or
          // while a moderator has suspended the account (#180).
          const account = await getAccount().catch(() => null);
          // Signed in after all: /account answered. Lift the sign-in gate,
          // or a link that just signed them in would land on it again.
          if (account?.status === "pending_deletion" || account?.suspended) setUnauthorized(false);
          // Asking to be deleted wins over a ban: it is what they last chose.
          if (account?.status === "pending_deletion") setPendingDeletion(account);
          else if (account?.suspended) setSuspended(true);
        }
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

  async function signOut() {
    forgetNoteLanguage();
    await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      headers: { Accept: "application/json" },
    }).catch(() => {});
    // Show the gate regardless. If the request failed the cookie may
    // survive, but the next auth probe settles it either way.
    setPendingDeletion(null);
    setSuspended(false);
    setUnauthorized(true);
  }

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

  if (pendingDeletion) {
    return (
      <div className="app">
        <PendingDeletion account={pendingDeletion} onKept={reprobeAuth} onSignOut={() => void signOut()}
          onUnauthorized={() => { setPendingDeletion(null); setUnauthorized(true); }} />
      </div>
    );
  }

  if (suspended) {
    return (
      <div className="app">
        <div className="empty-state pending-deletion">
          <h2>Your account has been suspended</h2>
          <p>A moderator has suspended this account. If you think that’s wrong, write to{" "}
            <a href="mailto:nammaindies@gmail.com">nammaindies@gmail.com</a>. You can still delete your account.</p>
          <button className="link-btn" onClick={() => setDeletingAccount(true)}>delete account</button>
          <button className="link-btn" onClick={() => void signOut()}>sign out</button>
        </div>
        {deletingAccount && (
          <DeleteAccount onClose={() => setDeletingAccount(false)}
            onScheduled={(account) => { setDeletingAccount(false); setSuspended(false); setPendingDeletion(account); }}
            onUnauthorized={() => { setDeletingAccount(false); setUnauthorized(true); }} />
        )}
      </div>
    );
  }

  return (
    <div className="app">
      <div className="topbar">
        {/* Hidden: hold the brand for two seconds in the app (or open ?haptics=check on the web). */}
        <span
          className="brand"
          onPointerDown={() => {
            if (!Capacitor.isNativePlatform()) return;
            window.clearTimeout(brandHold.current);
            brandHold.current = window.setTimeout(() => setHapticsCheck(true), HAPTICS_HOLD_MS);
          }}
          onPointerUp={() => window.clearTimeout(brandHold.current)}
          onPointerLeave={() => window.clearTimeout(brandHold.current)}
          onPointerCancel={() => window.clearTimeout(brandHold.current)}
          onContextMenu={(event) => { if (Capacitor.isNativePlatform()) event.preventDefault(); }}
        >
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
            await signOut();
          }}
        >
          sign out
        </button>
      </div>
      {/* Apple 5.1.1 wants the privacy policy reachable *inside* the app, not
          only in the store listing, and 1.2 wants published contact details for
          a UGC app. Both live on one line rather than behind a settings screen
          this app does not have. */}
      <div className="legal-row">
        <a href="https://nammaindies.org/privacy" target="_blank" rel="noreferrer">
          privacy
        </a>
        <span aria-hidden="true"> · </span>
        <a href="mailto:nammaindies@gmail.com">contact us</a>
        <span aria-hidden="true"> · </span>
        {/* Apple 1.2: blocking abusive users, and undoing it (#180). */}
        <button type="button" className="link-btn legal-link" onClick={() => setShowingBlocks(true)}>blocked people</button>
        <span aria-hidden="true"> · </span>
        {/* Apple 5.1.1(v): deleting the account has to be in the app, findable. */}
        <button type="button" className="link-btn legal-link" onClick={() => setDeletingAccount(true)}>delete account</button>
      </div>
      {deletingAccount && (
        <DeleteAccount onClose={() => setDeletingAccount(false)}
          onScheduled={(account) => { setDeletingAccount(false); setPendingDeletion(account); }}
          onUnauthorized={() => { setDeletingAccount(false); setUnauthorized(true); }} />
      )}
      {showingBlocks && (
        <BlockedPeople onClose={() => setShowingBlocks(false)} onChanged={visibilityChanged}
          onUnauthorized={() => { setShowingBlocks(false); setUnauthorized(true); }} />
      )}
      <div className="screen">
        {tab === "capture" || (tab === "admin" && !isModerator) ? (
          <Capture />
        ) : tab === "admin" ? (
          <Admin onUnauthorized={() => setUnauthorized(true)} />
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
        {isModerator && (
          <button className={tab === "admin" ? "active" : ""} onClick={() => setTab("admin")}>
            <span className="icon">🛠️</span>
            ADMIN
          </button>
        )}
      </div>
      <div className="legal-row"><a href={`${API_BASE}/grievances`}>Grievances</a></div>
      {hapticsCheck && (
        <HapticsCheck
          onClose={() => {
            setHapticsCheck(false);
            const url = new URL(window.location.href);
            if (url.searchParams.get(HAPTICS_CHECK_PARAM) === "check") {
              url.searchParams.delete(HAPTICS_CHECK_PARAM);
              window.history.replaceState(null, "", url);
            }
          }}
        />
      )}
      {showFailed && (
        <FailedSightings
          onClose={() => {
            setShowFailed(false);
            failedCount().then(setFailed);
          }}
        />
      )}
      {/* App-level, not inside Capture: a queue that catches up on reconnect
          finishes its uploads while the person is on the map or the dex, and
          that success deserves the same acknowledgement as one they watched. */}
      <Celebration />
    </div>
  );
}
