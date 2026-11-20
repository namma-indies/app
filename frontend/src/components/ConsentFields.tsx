import { useId } from "react";
import { API_BASE } from "../apiBase";
import type { ConsentAnswers, ConsentPolicy } from "../consentApi";
import "./Consent.css";

export default function ConsentFields({ policy, answers, onChange, disabled = false, legend = "Before creating your account" }: {
  policy: ConsentPolicy;
  answers: ConsentAnswers;
  onChange: (answers: ConsentAnswers) => void;
  disabled?: boolean;
  legend?: string;
}) {
  const id = useId();
  return (
    <fieldset className="consent-fields" disabled={disabled}>
      <legend>{legend}</legend>
      <label htmlFor={`${id}-terms`}>
        <input id={`${id}-terms`} type="checkbox" required checked={answers.agree_terms_and_guidelines}
          onChange={(event) => onChange({ ...answers, agree_terms_and_guidelines: event.target.checked })} />
        <span>I agree to the <a href={`${API_BASE}/policies/terms`} target="_blank" rel="noreferrer">Terms of Service</a>{" "}
          and <a href={`${API_BASE}/policies/community-guidelines`} target="_blank" rel="noreferrer">Community Guidelines</a>.</span>
      </label>
      <label htmlFor={`${id}-privacy`}>
        <input id={`${id}-privacy`} type="checkbox" required checked={answers.acknowledge_privacy}
          onChange={(event) => onChange({ ...answers, acknowledge_privacy: event.target.checked })} />
        <span>I acknowledge the <a href={`${API_BASE}/policies/privacy`} target="_blank" rel="noreferrer">Privacy Policy</a>.</span>
      </label>
      {policy.minimum_age !== null && (
        <label htmlFor={`${id}-age`}>
          <input id={`${id}-age`} type="checkbox" required checked={answers.age_confirmed}
            onChange={(event) => onChange({ ...answers, age_confirmed: event.target.checked })} />
          <span>I am at least {policy.minimum_age} years old.</span>
        </label>
      )}
    </fieldset>
  );
}
