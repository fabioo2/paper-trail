import { LoaderCircle, RotateCcw } from "lucide-react";
import { useRef } from "react";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { useRetryReceipt } from "@/features/receipts/api";

const RETRY_ERROR_TOAST = "receipt-retry-error";

function retryErrorMessage(error: Error): string {
  if (error instanceof ApiError && error.status === 409) {
    return "This receipt is already being read again, or can't be.";
  }
  if (error instanceof ApiError) {
    return "Something went wrong on the server. Tap Try again to retry.";
  }
  return "Check your connection, then tap Try again.";
}

interface RetryButtonProps {
  receiptId: number;
  variant?: "default" | "outline";
}

/** Sends a FAILED receipt back to be read (`retryReceipt`), once per tap. */
export function RetryButton({
  receiptId,
  variant = "default",
}: RetryButtonProps) {
  const retry = useRetryReceipt(receiptId);
  // isPending and the receipt's new status only show after a re-render; a second tap in
  // between must not retry twice. After a success the button goes away with the FAILED status.
  const sending = useRef(false);

  const send = () => {
    if (sending.current) return;
    sending.current = true;
    retry.mutate(undefined, {
      onSuccess: () => {
        toast.dismiss(RETRY_ERROR_TOAST);
      },
      onError: (error) => {
        sending.current = false;
        toast.error("Couldn't try again", {
          id: RETRY_ERROR_TOAST,
          description: retryErrorMessage(error),
        });
      },
    });
  };

  return (
    <Button
      size="lg"
      variant={variant}
      className="h-12 w-full"
      disabled={retry.isPending}
      onClick={send}
    >
      {retry.isPending ? (
        <LoaderCircle aria-hidden="true" className="animate-spin" />
      ) : (
        <RotateCcw aria-hidden="true" />
      )}
      Try again
    </Button>
  );
}
