import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  Camera,
  CircleCheck,
  CircleX,
  Clock,
  CloudOff,
  Images,
  LoaderCircle,
  type LucideIcon,
  ReceiptText,
  ScanLine,
  SearchX,
  Send,
  TriangleAlert,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  isFinalStatus,
  type ReceiptDetailOut,
  READING_TIMEOUT_MS,
  type ReceiptUploadOut,
  refetchAfterReading,
  watchReading,
  useReceiptProgress,
  useUploadReceipt,
} from "@/features/receipts/api";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { failureMessage } from "@/features/receipts/failure-reasons";
import { RetryButton } from "@/features/receipts/receipt-failure";
import { cn } from "@/lib/utils";

const UPLOAD_ERROR_TOAST = "scan-upload-error";

interface Photo {
  file: File;
  url: string;
}

type Step =
  | { kind: "pick" }
  | { kind: "picked"; photo: Photo }
  | { kind: "tracking"; receiptId: number }
  | { kind: "duplicate"; receiptId: number };

/** The Scan tab: pick or take a photo, send it, and follow the receipt until it is read. */
export function ScanReceipt() {
  const [step, setStep] = useState<Step>({ kind: "pick" });
  const photo = step.kind === "picked" ? step.photo : null;

  useEffect(() => {
    if (photo === null) return;
    return () => {
      URL.revokeObjectURL(photo.url);
    };
  }, [photo]);

  const startOver = () => {
    setStep({ kind: "pick" });
  };

  switch (step.kind) {
    case "pick":
      return (
        <PickPhoto
          onPick={(file) => {
            setStep({
              kind: "picked",
              photo: { file, url: URL.createObjectURL(file) },
            });
          }}
        />
      );
    case "picked":
      return (
        <SendPhoto
          photo={step.photo}
          onSent={(receipt, duplicate) => {
            // A duplicate of a receipt that failed is followed like a new one, to retry it.
            setStep({
              kind:
                duplicate && receipt.status !== "failed"
                  ? "duplicate"
                  : "tracking",
              receiptId: receipt.id,
            });
          }}
          onDiscard={startOver}
        />
      );
    case "tracking":
      return (
        <ReceiptProgress receiptId={step.receiptId} onStartOver={startOver} />
      );
    case "duplicate":
      return (
        <StatusCard
          icon={ReceiptText}
          iconClassName="text-primary"
          title="You've already scanned this receipt"
          description="This photo was sent before, so Paper Trail kept the receipt it already has."
        >
          <ReceiptLink receiptId={step.receiptId}>Open receipt</ReceiptLink>
          <ScanAnother onClick={startOver} />
        </StatusCard>
      );
  }
}

function PickPhoto({ onPick }: { onPick: (file: File) => void }) {
  return (
    <EmptyState
      icon={ScanLine}
      title="Scan a receipt"
      description="Photograph a receipt and Paper Trail reads the store, items and prices for you."
    >
      <div className="mt-4 flex w-full flex-col gap-3">
        <PhotoInput icon={Camera} capture onPick={onPick}>
          Take a photo
        </PhotoInput>
        <PhotoInput icon={Images} variant="outline" onPick={onPick}>
          Choose from library
        </PhotoInput>
      </div>
    </EmptyState>
  );
}

interface PhotoInputProps {
  icon: LucideIcon;
  capture?: boolean;
  variant?: "default" | "outline";
  onPick: (file: File) => void;
  children: ReactNode;
}

function PhotoInput({
  icon: Icon,
  capture = false,
  variant = "default",
  onPick,
  children,
}: PhotoInputProps) {
  return (
    <Button
      asChild
      size="lg"
      variant={variant}
      className="h-12 w-full cursor-pointer text-base has-focus-visible:ring-[3px] has-focus-visible:ring-ring/50"
    >
      <label>
        <Icon aria-hidden="true" />
        {children}
        <input
          type="file"
          accept="image/*"
          capture={capture ? "environment" : undefined}
          className="sr-only"
          onChange={(event) => {
            const file = event.target.files?.[0];
            // Cleared so picking the same photo again still fires a change.
            event.target.value = "";
            if (file) onPick(file);
          }}
        />
      </label>
    </Button>
  );
}

interface SendPhotoProps {
  photo: Photo;
  onSent: (receipt: ReceiptUploadOut, duplicate: boolean) => void;
  onDiscard: () => void;
}

function SendPhoto({ photo, onSent, onDiscard }: SendPhotoProps) {
  const upload = useUploadReceipt();
  // isPending only flips after a re-render; a second tap in between must not upload twice.
  const sending = useRef(false);

  const send = () => {
    if (sending.current) return;
    sending.current = true;
    upload.mutate(photo.file, {
      onSuccess: ({ receipt, duplicate }) => {
        toast.dismiss(UPLOAD_ERROR_TOAST);
        onSent(receipt, duplicate);
      },
      onError: (error) => {
        const { title, description } = uploadErrorMessage(error);
        toast.error(title, { id: UPLOAD_ERROR_TOAST, description });
      },
      onSettled: () => {
        sending.current = false;
      },
    });
  };

  return (
    <section className="mx-auto flex max-w-sm flex-col items-center gap-4 px-4 py-6">
      <h1 className="text-xl font-semibold tracking-tight">Send this photo?</h1>
      <img
        src={photo.url}
        alt="The receipt you picked"
        className="max-h-80 w-auto max-w-full rounded-xl border object-contain"
      />
      <Button
        size="lg"
        className="h-12 w-full text-base"
        disabled={upload.isPending}
        onClick={send}
      >
        {upload.isPending ? (
          <>
            <LoaderCircle aria-hidden="true" className="animate-spin" />
            Uploading…
          </>
        ) : (
          <>
            <Send aria-hidden="true" />
            Send
          </>
        )}
      </Button>
      <Button variant="ghost" disabled={upload.isPending} onClick={onDiscard}>
        Pick a different photo
      </Button>
    </section>
  );
}

function uploadErrorMessage(error: Error): {
  title: string;
  description: string;
} {
  if (error instanceof ApiError && error.status === 413) {
    return {
      title: "This photo is too large",
      description: "Choose a smaller photo, or retake it at a lower quality.",
    };
  }
  if (error instanceof ApiError && error.status === 415) {
    return {
      title: "Paper Trail can't read this file",
      description: "Choose a JPEG, PNG, WebP or HEIC photo of the receipt.",
    };
  }
  if (error instanceof ApiError) {
    return {
      title: "Couldn't send the photo",
      description: "Something went wrong on the server. Tap Send to try again.",
    };
  }
  return {
    title: "Couldn't send the photo",
    description: "Check your connection, then tap Send to try again.",
  };
}

function ReceiptProgress({
  receiptId,
  onStartOver,
}: {
  receiptId: number;
  onStartOver: () => void;
}) {
  const [timedOut, setTimedOut] = useState(false);
  // Each read (the first, then each Try again) gets its own READING_TIMEOUT_MS.
  const [attempt, setAttempt] = useState(0);
  const [wasSettled, setWasSettled] = useState(false);
  const progress = useReceiptProgress(receiptId, !timedOut);
  const receipt = progress.data;
  const settled = receipt !== undefined && isFinalStatus(receipt.status);
  if (settled !== wasSettled) {
    setWasSettled(settled);
    if (!settled) {
      setTimedOut(false);
      setAttempt((count) => count + 1);
    }
  }
  const queryClient = useQueryClient();

  useEffect(() => {
    const timer = setTimeout(() => {
      setTimedOut(true);
    }, READING_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [attempt]);

  useEffect(() => {
    if (settled) {
      void refetchAfterReading(queryClient);
    }
  }, [settled, queryClient]);

  if (!settled && progress.isError) {
    return progress.error instanceof ApiError &&
      progress.error.status === 404 ? (
      <StatusCard
        icon={SearchX}
        iconClassName="text-muted-foreground"
        title="Couldn't find this receipt"
        description="It may have been removed. Your other receipts are in Receipts."
      >
        <ReceiptsLink />
      </StatusCard>
    ) : (
      <StatusCard
        icon={CloudOff}
        iconClassName="text-destructive"
        title="Lost connection — check Receipts later"
        description="Paper Trail couldn't check on the receipt, but it is still being read."
      >
        <Button
          size="lg"
          className="h-12 w-full"
          disabled={progress.isFetching}
          onClick={() => {
            watchReading(queryClient, receiptId, {
              status: "processing",
              stage: receipt?.stage ?? "queued",
            });
            void progress.refetch();
          }}
        >
          {progress.isFetching ? (
            <LoaderCircle aria-hidden="true" className="animate-spin" />
          ) : null}
          Try again
        </Button>
        <ReceiptsLink />
      </StatusCard>
    );
  }

  if (!settled) {
    return timedOut ? (
      <StatusCard
        icon={Clock}
        iconClassName="text-muted-foreground"
        title="Still working — check Receipts later"
        description="Reading is taking longer than usual. The receipt shows up in Receipts once it's done."
      >
        <ReceiptsLink />
      </StatusCard>
    ) : (
      <StatusCard
        icon={LoaderCircle}
        iconClassName="animate-spin text-primary"
        title="Reading your receipt — about 20 seconds"
        description="You can leave this page; the receipt shows up in Receipts once it's read."
      />
    );
  }

  switch (receipt.status) {
    case "completed":
      return (
        <StatusCard
          icon={CircleCheck}
          iconClassName="text-success"
          title="Receipt saved"
          description="Paper Trail read the receipt and the numbers add up."
        >
          <ReceiptFacts receipt={receipt} />
          <ReceiptLink receiptId={receipt.id}>View receipt</ReceiptLink>
          <ScanAnother onClick={onStartOver} />
        </StatusCard>
      );
    case "needs_review":
      return (
        <StatusCard
          icon={TriangleAlert}
          iconClassName="text-warning"
          title="Needs a second look"
          description="Paper Trail read the receipt, but some numbers don't add up. Check them before relying on it."
        >
          <ReceiptFacts receipt={receipt} />
          <ReceiptLink receiptId={receipt.id}>Review receipt</ReceiptLink>
          <ScanAnother onClick={onStartOver} />
        </StatusCard>
      );
    default: {
      const retake = receipt.failure_reason === "unreadable_photo";
      return (
        <StatusCard
          icon={CircleX}
          iconClassName="text-destructive"
          title="Couldn't read this receipt"
          description={failureMessage(receipt.failure_reason)}
        >
          {retake ? (
            <Button size="lg" className="h-12 w-full" onClick={onStartOver}>
              Retake the photo
            </Button>
          ) : null}
          <RetryButton
            receiptId={receipt.id}
            variant={retake ? "outline" : "default"}
          />
          {retake ? null : <ScanAnother onClick={onStartOver} />}
        </StatusCard>
      );
    }
  }
}

interface StatusCardProps {
  icon: LucideIcon;
  iconClassName: string;
  title: string;
  description: string;
  children?: ReactNode;
}

function StatusCard({
  icon: Icon,
  iconClassName,
  title,
  description,
  children,
}: StatusCardProps) {
  return (
    <section aria-live="polite" className="mx-auto max-w-sm px-4 py-6">
      <Card className="gap-4 px-5 py-5">
        <div className="flex items-start gap-3">
          <Icon
            aria-hidden="true"
            className={cn("mt-0.5 size-6 shrink-0", iconClassName)}
          />
          <div className="flex flex-col gap-1">
            <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
            <p className="text-sm text-muted-foreground">{description}</p>
          </div>
        </div>
        {children}
      </Card>
    </section>
  );
}

function ReceiptFacts({ receipt }: { receipt: ReceiptDetailOut }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      <dt className="text-muted-foreground">Store</dt>
      <dd className="text-right break-words">{receipt.store_name}</dd>
      <dt className="text-muted-foreground">Date</dt>
      <dd className="text-right">
        {receipt.purchased_on === null
          ? "Date unknown"
          : formatDate(receipt.purchased_on)}
      </dd>
      <dt className="text-muted-foreground">Items</dt>
      <dd className="text-right tabular-nums">{receipt.item_count}</dd>
      <dt className="font-semibold">Total</dt>
      <dd className="text-right font-semibold tabular-nums">
        {receipt.total === null ? "—" : formatMoney(receipt.total)}
      </dd>
    </dl>
  );
}

function ReceiptLink({
  receiptId,
  children,
}: {
  receiptId: number;
  children: ReactNode;
}) {
  return (
    <Button asChild size="lg" className="h-12 w-full">
      <Link to="/receipts/$receiptId" params={{ receiptId: String(receiptId) }}>
        {children}
      </Link>
    </Button>
  );
}

function ReceiptsLink() {
  return (
    <Button asChild variant="outline" size="lg" className="h-12 w-full">
      <Link to="/receipts">Go to Receipts</Link>
    </Button>
  );
}

function ScanAnother({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="ghost" onClick={onClick}>
      Scan another receipt
    </Button>
  );
}
