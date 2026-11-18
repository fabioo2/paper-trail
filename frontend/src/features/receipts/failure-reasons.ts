import type { FailureReason } from "@/features/receipts/api";

/** Why a receipt failed, in words a person can act on. */
export const FAILURE_MESSAGES: Record<FailureReason, string> = {
  budget_reached:
    "This month's Claude budget is used up. Raise the budget or wait until next month, then try again.",
  claude_unavailable:
    "Claude couldn't read it just now. Try again in a minute.",
  no_api_key:
    "No Claude API key is set on the server. Add ANTHROPIC_API_KEY to .env, then try again.",
  unreadable_photo:
    "The photo couldn't be read. Retake it with the whole receipt flat, in focus and well lit.",
  stopped_unexpectedly: "Paper Trail stopped while reading it. Try again.",
  unknown: "Something went wrong while reading it. Try again.",
};

export function failureMessage(reason: FailureReason | null): string {
  return FAILURE_MESSAGES[reason ?? "unknown"];
}
