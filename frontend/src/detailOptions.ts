import type { Condition, EarNotch, Sex } from "./api";

/** The choices for each animal detail, shared by the capture form and the
 * suggested-details prompt so the two can never offer different options. */
export const SEX_OPTIONS: { value: Sex; label: string }[] = [
  { value: "male", label: "♂ male" },
  { value: "female", label: "♀ female" },
  { value: "unsure", label: "unsure" },
];

export const EAR_NOTCH_OPTIONS: { value: EarNotch; label: string }[] = [
  { value: "none", label: "none" },
  { value: "left", label: "left" },
  { value: "right", label: "right" },
  { value: "unsure", label: "unsure" },
];

export const CONDITION_OPTIONS: { value: Condition; label: string }[] = [
  { value: "healthy", label: "healthy" },
  { value: "injured", label: "injured" },
  { value: "unsure", label: "unsure" },
];
