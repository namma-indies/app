import type { ReactNode } from "react";
import { API_BASE } from "../apiBase";

/** Stable server links let published documents change without a native rebuild. */
export default function LegalLinks({ children }: { children?: ReactNode }) {
  return (
    <nav className="legal-row" aria-label="Policies and support">
      <a href={`${API_BASE}/policies/terms`} target="_blank" rel="noreferrer">Terms of Service</a>
      <a href={`${API_BASE}/policies/community-guidelines`} target="_blank" rel="noreferrer">Community Guidelines</a>
      <a href={`${API_BASE}/policies/privacy`} target="_blank" rel="noreferrer">Privacy Policy</a>
      <a href={`${API_BASE}/policies/grievance`} target="_blank" rel="noreferrer">Grievances</a>
      <a href={`${API_BASE}/support`} target="_blank" rel="noreferrer">Support</a>
      {children}
    </nav>
  );
}
