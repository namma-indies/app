import { useState } from "react";
import LegalLinks from "./LegalLinks";

/** Everything about your account and the rules, behind one button.
 *
 * These used to be a row of seven links under the logo on every screen, so
 * the first thing anyone read was "Terms of Service". They have to stay
 * findable inside the app (Apple 5.1.1: privacy policy and account deletion;
 * 1.2: blocking and contact; the IT Rules: the grievance officer), and a menu
 * labelled MENU in the corner of every screen is where people look for them.
 */
export default function AccountMenu({
  onBlocked,
  onDeleteAccount,
  onSignOut,
}: {
  onBlocked: () => void;
  onDeleteAccount: () => void;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const then = (act: () => void) => () => { setOpen(false); act(); };

  return (
    <>
      <button className="menu-btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>
        menu
      </button>
      {open && (
        <div className="viewer-overlay" onClick={() => setOpen(false)}>
          <div className="report-sheet account-menu" role="dialog" aria-label="Menu" onClick={(e) => e.stopPropagation()}>
            <div className="account-menu-actions">
              <button className="link-btn" onClick={then(onBlocked)}>Blocked people</button>
              <button className="link-btn" onClick={then(onDeleteAccount)}>Delete account</button>
              <button
                className="link-btn"
                onClick={() => {
                  // Confirmed: a stray tap costs a round trip through email.
                  if (!confirm("Sign out of indiedex?")) return;
                  setOpen(false);
                  onSignOut();
                }}
              >
                Sign out
              </button>
            </div>
            <LegalLinks />
            <div className="actions-row">
              <button className="btn btn-secondary" onClick={() => setOpen(false)}>CLOSE</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
