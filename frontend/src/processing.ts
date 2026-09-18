export const UPLOAD_COMPLETE_EVENT = "indiedex:uploaded";

export function isProcessing(state?: string): boolean {
  return state === "queued" || state === "processing";
}

export function processingLabel(state?: string): string | null {
  switch (state) {
    case "queued": return "Uploaded · waiting to process";
    case "processing": return "Uploaded · processing media";
    case "needs_review": return "Private · review animal associations";
    case "no_animal": return "No animal detected · matching unavailable";
    case "failed": return "Processing failed · upload saved";
    default: return null;
  }
}
