import { Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  CircleCheck,
  Plus,
  SearchX,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { type ReactNode, type SyntheticEvent, useId, useState } from "react";
import { toast } from "sonner";

import { ApiError, apiBaseUrl } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  INCONSISTENT_RECEIPT,
  type ResolveReviewIn,
  type ReviewOut,
  useResolveReview,
  useReview,
} from "@/features/reviews/api";
import {
  type AdjustmentRow,
  draftOf,
  emptyItem,
  type FieldErrors,
  fieldsOf,
  hasDateIssue,
  type ItemRow,
  reasonText,
} from "@/features/reviews/draft-form";

const DEFAULT_CURRENCY = "CAD";

/** One review: the photo, why it waits, and its reading to correct or accept. */
export function ReviewEditor({ reviewId }: { reviewId: number }) {
  const review = useReview(reviewId);

  if (review.isPending) return <ReviewEditorSkeleton />;
  if (review.isError) {
    const notFound =
      review.error instanceof ApiError && review.error.status === 404;
    return notFound ? (
      <ReviewNotFound />
    ) : (
      <EmptyState
        icon={TriangleAlert}
        title="Couldn't open this review"
        description="Paper Trail could not reach the server. Pull to refresh or try again later."
      />
    );
  }
  if (review.data.status !== "open" || review.data.answer_queued) {
    return (
      <EmptyState
        icon={CircleCheck}
        title="Already answered"
        description="This receipt has been checked. Paper Trail saves it in the background."
      >
        <Button asChild variant="outline">
          <Link to="/review">Back to reviews</Link>
        </Button>
      </EmptyState>
    );
  }
  return <ReviewForm key={review.data.id} review={review.data} />;
}

export function ReviewNotFound() {
  return (
    <EmptyState
      icon={SearchX}
      title="Review not found"
      description="This review does not exist. It may have been answered already, or the link is wrong."
    />
  );
}

function ReviewForm({ review }: { review: ReviewOut }) {
  const [initial] = useState(() => fieldsOf(review.draft));
  const [fields, setFields] = useState(initial);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [reason, setReason] = useState("");
  const resolve = useResolveReview(review.id);
  const navigate = useNavigate();
  const busy = resolve.isPending;
  const edited = JSON.stringify(fields) !== JSON.stringify(initial);
  const currency = review.draft?.currency ?? DEFAULT_CURRENCY;
  const dateIssue = hasDateIssue(review.issues);
  const problem =
    resolve.error instanceof ApiError ? resolve.error.problem : undefined;
  const inconsistent = problem?.type === INCONSISTENT_RECEIPT;

  const send = (body: ResolveReviewIn, message: string) => {
    resolve.mutate(body, {
      onSuccess: () => {
        toast.success(message);
        void navigate({ to: "/review" });
      },
    });
  };
  const sendDraft = (forced: Pick<ResolveReviewIn, "force" | "reason">) => {
    const result = draftOf(fields, currency);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    send({ accept: false, draft: result.draft, ...forced }, "Receipt saved");
  };
  const save = (event: SyntheticEvent) => {
    event.preventDefault();
    sendDraft({ force: false });
  };
  const accept = () => {
    send({ accept: true, force: false }, "Reading accepted");
  };
  const saveAnyway = () => {
    const forced = { force: true, reason: reason.trim() };
    if (edited || review.draft === null) sendDraft(forced);
    else send({ accept: true, ...forced }, "Receipt saved");
  };
  const set =
    (key: "storeName" | "purchasedOn" | "subtotal" | "tax" | "total") =>
    (event: { target: { value: string } }) => {
      setFields((current) => ({ ...current, [key]: event.target.value }));
    };
  const setItem = (key: number, change: Partial<ItemRow>) => {
    setFields((current) => ({
      ...current,
      items: current.items.map((row) =>
        row.key === key ? { ...row, ...change } : row,
      ),
    }));
  };
  const setAdjustment = (key: number, change: Partial<AdjustmentRow>) => {
    setFields((current) => ({
      ...current,
      adjustments: current.adjustments.map((row) =>
        row.key === key ? { ...row, ...change } : row,
      ),
    }));
  };

  const dateField = (
    <Field label="Purchase date">
      {(props) => (
        <Input
          {...props}
          type="date"
          // eslint-disable-next-line jsx-a11y/no-autofocus -- a date issue is what this screen is for
          autoFocus={dateIssue}
          value={fields.purchasedOn}
          onChange={set("purchasedOn")}
          className={cn("h-11", dateIssue && "border-warning")}
        />
      )}
    </Field>
  );

  return (
    <article className="flex flex-col gap-4 px-4 py-4">
      <Link
        to="/review"
        className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Review
      </Link>
      <h1 className="text-xl font-semibold tracking-tight break-words">
        {review.draft === null || review.draft.store_name === ""
          ? "Unreadable receipt"
          : review.draft.store_name}
      </h1>

      <section
        aria-labelledby="issues-heading"
        className="flex flex-col gap-2 rounded-xl border border-warning bg-warning/10 p-4"
      >
        <h2 id="issues-heading" className="font-medium">
          {reasonText(review)}
        </h2>
        {review.issues.length > 0 ? (
          <ul className="list-disc pl-5 text-sm">
            {review.issues.map((issue, index) => (
              <li key={index}>{issue.message}</li>
            ))}
          </ul>
        ) : null}
        {dateIssue ? dateField : null}
      </section>

      {review.image_url === null ? null : (
        <a
          href={new URL(review.image_url, apiBaseUrl).toString()}
          target="_blank"
          rel="noreferrer"
          className="flex flex-col gap-1"
        >
          <img
            src={new URL(review.image_url, apiBaseUrl).toString()}
            alt="The receipt"
            className="max-h-96 w-full rounded-xl border bg-muted object-contain"
          />
          <span className="text-center text-xs text-muted-foreground">
            Tap the photo to open it full size
          </span>
        </a>
      )}

      <form className="flex flex-col gap-4" onSubmit={save} noValidate>
        {dateIssue ? null : dateField}
        <Field label="Store">
          {(props) => (
            <Input
              {...props}
              value={fields.storeName}
              onChange={set("storeName")}
            />
          )}
        </Field>

        <fieldset className="flex flex-col gap-3">
          <legend className="mb-2 text-sm font-medium">Items</legend>
          {fields.items.map((row, index) => (
            <ItemFields
              key={row.key}
              row={row}
              number={index + 1}
              errors={errors}
              canRemove={fields.items.length > 1}
              onChange={(change) => {
                setItem(row.key, change);
              }}
              onRemove={() => {
                setFields((current) => ({
                  ...current,
                  items: current.items.filter((item) => item.key !== row.key),
                }));
              }}
            />
          ))}
          <Button
            type="button"
            variant="outline"
            className="w-fit"
            onClick={() => {
              setFields((current) => ({
                ...current,
                items: [...current.items, emptyItem()],
              }));
            }}
          >
            <Plus aria-hidden="true" />
            Add item
          </Button>
        </fieldset>

        {fields.adjustments.length === 0 ? null : (
          <fieldset className="flex flex-col gap-3">
            <legend className="mb-2 text-sm font-medium">
              Deposits, fees and receipt discounts
            </legend>
            {fields.adjustments.map((row) => (
              <Field
                key={row.key}
                label={row.description || row.kind}
                error={errors[`adjustments.${String(row.key)}.amount`]}
              >
                {(props) => (
                  <Input
                    {...props}
                    inputMode="decimal"
                    value={row.amount}
                    onChange={(event) => {
                      setAdjustment(row.key, { amount: event.target.value });
                    }}
                  />
                )}
              </Field>
            ))}
          </fieldset>
        )}

        <div className="grid grid-cols-3 gap-2">
          <Field label="Subtotal" error={errors.subtotal}>
            {(props) => (
              <Input
                {...props}
                inputMode="decimal"
                value={fields.subtotal}
                onChange={set("subtotal")}
              />
            )}
          </Field>
          <Field label="Tax" error={errors.tax}>
            {(props) => (
              <Input
                {...props}
                inputMode="decimal"
                value={fields.tax}
                onChange={set("tax")}
              />
            )}
          </Field>
          <Field label="Total" error={errors.total}>
            {(props) => (
              <Input
                {...props}
                inputMode="decimal"
                value={fields.total}
                onChange={set("total")}
              />
            )}
          </Field>
        </div>

        {Object.keys(errors).length > 0 ? (
          <p role="alert" className="text-sm text-destructive">
            Some fields need fixing before the receipt can be saved.
          </p>
        ) : null}
        {inconsistent ? (
          <section
            role="alert"
            aria-labelledby="refused-heading"
            className="flex flex-col gap-2 rounded-xl border border-destructive p-4"
          >
            <h2 id="refused-heading" className="font-medium">
              The receipt still doesn't add up
            </h2>
            <ul className="list-disc pl-5 text-sm">
              {(problem.errors ?? []).map((error, index) => (
                <li key={index}>{error.msg}</li>
              ))}
            </ul>
            <Field label="Why keep it anyway?">
              {(props) => (
                <Input
                  {...props}
                  placeholder="Torn receipt"
                  value={reason}
                  onChange={(event) => {
                    setReason(event.target.value);
                  }}
                />
              )}
            </Field>
            <Button
              type="button"
              variant="destructive"
              disabled={busy || reason.trim() === ""}
              onClick={saveAnyway}
            >
              Save anyway
            </Button>
          </section>
        ) : resolve.error ? (
          <p role="alert" className="text-sm text-destructive">
            {resolve.error.message}
          </p>
        ) : null}

        <div className="flex flex-col gap-2 sm:flex-row-reverse">
          <Button type="submit" size="lg" disabled={busy}>
            Save
          </Button>
          {review.draft === null ? null : (
            <Button
              type="button"
              size="lg"
              variant="outline"
              disabled={busy || edited}
              onClick={accept}
            >
              Accept as read
            </Button>
          )}
        </div>
      </form>
    </article>
  );
}

function ItemFields({
  row,
  number,
  errors,
  canRemove,
  onChange,
  onRemove,
}: {
  row: ItemRow;
  number: number;
  errors: FieldErrors;
  canRemove: boolean;
  onChange: (change: Partial<ItemRow>) => void;
  onRemove: () => void;
}) {
  const error = (field: string) => errors[`items.${String(row.key)}.${field}`];
  return (
    <fieldset className="flex flex-col gap-2 rounded-xl border bg-card p-3">
      <legend className="sr-only">{`Item ${String(number)}`}</legend>
      <div className="flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <Field label="Description" error={error("description")}>
            {(props) => (
              <Input
                {...props}
                value={row.description}
                onChange={(event) => {
                  onChange({ description: event.target.value });
                }}
              />
            )}
          </Field>
        </div>
        {canRemove ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove item ${String(number)}`}
            onClick={onRemove}
          >
            <Trash2 aria-hidden="true" />
          </Button>
        ) : null}
      </div>
      <div className="grid grid-cols-3 gap-2">
        <Field label="Qty" error={error("quantity")}>
          {(props) => (
            <Input
              {...props}
              inputMode="decimal"
              value={row.quantity}
              onChange={(event) => {
                onChange({ quantity: event.target.value });
              }}
            />
          )}
        </Field>
        <Field label="Paid" error={error("paid")}>
          {(props) => (
            <Input
              {...props}
              inputMode="decimal"
              value={row.paid}
              onChange={(event) => {
                onChange({ paid: event.target.value });
              }}
            />
          )}
        </Field>
        <Field label="Discount" error={error("discount")}>
          {(props) => (
            <Input
              {...props}
              inputMode="decimal"
              placeholder="0.00"
              value={row.discount}
              onChange={(event) => {
                onChange({ discount: event.target.value });
              }}
            />
          )}
        </Field>
      </div>
    </fieldset>
  );
}

interface InputProps {
  id: string;
  "aria-invalid"?: true;
  "aria-describedby"?: string;
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string | undefined;
  children: (props: InputProps) => ReactNode;
}) {
  const id = useId();
  const errorId = `${id}-error`;
  const props: InputProps =
    error === undefined
      ? { id }
      : { id, "aria-invalid": true, "aria-describedby": errorId };
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children(props)}
      {error === undefined ? null : (
        <p id={errorId} className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

function ReviewEditorSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading review"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-20 w-full" />
      <Skeleton className="h-64 w-full" />
      <Skeleton className="h-9 w-full" />
    </div>
  );
}
