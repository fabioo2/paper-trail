import { useIsMutating } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  ArrowLeft,
  Check,
  CheckCheck,
  CloudOff,
  RotateCcw,
  TriangleAlert,
  X,
} from "lucide-react";
import { type SyntheticEvent, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type Category,
  type FieldSourceKey,
  productKeys,
  type ProductOut,
  type ProductToCheckOut,
  type ProductUpdateIn,
  isStale,
  shownValues,
  sizeText,
  STALE_MESSAGE,
  useCategories,
  useConfirmProduct,
  useProduct,
  useProductsToCheck,
  useUpdateProduct,
} from "@/features/products/api";
import { FieldSourceMark } from "@/features/products/field-source";

const FIELDS: readonly { key: FieldSourceKey; label: string }[] = [
  { key: "canonical_name", label: "Name" },
  { key: "brand", label: "Brand" },
  { key: "size", label: "Size" },
  { key: "category", label: "Category" },
  { key: "tags", label: "Tags" },
];

/** What rejecting a guess sends: the field cleared. Name and category are edited instead. */
const CLEARED: Partial<Record<FieldSourceKey, ProductUpdateIn>> = {
  brand: { brand: "" },
  size: { size: "" },
  tags: { tags: [] },
};

/** The products nobody has confirmed, one at a time, in the order they were first loaded. */
export function ProductCheck() {
  const toCheck = useProductsToCheck();

  if (toCheck.isPending) return <ProductCheckSkeleton />;
  if (toCheck.isError) {
    return (
      <EmptyState
        icon={CloudOff}
        title="Products to check are unavailable"
        description="Paper Trail could not reach the server. Try again once you are back online."
      />
    );
  }
  return <CheckQueue fresh={toCheck.data} />;
}

/** The queue as the person walks it, after a fresh read of the list: the products already shown
 * and the current one stay where they are, later ones no longer listed go, and newly listed
 * ones join the end. */
function reconciled(
  rows: ProductToCheckOut[],
  index: number,
  fresh: ProductToCheckOut[],
): ProductToCheckOut[] {
  const listed = new Set(fresh.map((row) => row.product_id));
  const kept = rows.filter(
    (row, at) => at <= index || listed.has(row.product_id),
  );
  const known = new Set(kept.map((row) => row.product_id));
  return [...kept, ...fresh.filter((row) => !known.has(row.product_id))];
}

function CheckQueue({ fresh }: { fresh: ProductToCheckOut[] }) {
  const [rows, setRows] = useState(fresh);
  const [read, setRead] = useState(fresh);
  const [index, setIndex] = useState(0);
  const [skipped, setSkipped] = useState(0);
  if (fresh !== read) {
    setRead(fresh);
    setRows(reconciled(rows, index, fresh));
  }
  const row = rows[index];

  if (!row && skipped > 0) {
    return (
      <EmptyState
        icon={RotateCcw}
        title="End of the list"
        description={`${String(skipped)} skipped. Start again to go back to the products still waiting.`}
      >
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            setRows(fresh);
            setIndex(0);
            setSkipped(0);
          }}
        >
          Start again
        </Button>
      </EmptyState>
    );
  }
  if (!row) {
    return (
      <EmptyState
        icon={CheckCheck}
        title="All checked"
        description="Every product Claude guessed has been looked at. New ones show up here after you scan."
      >
        <Button asChild variant="outline">
          <Link to="/products">Back to products</Link>
        </Button>
      </EmptyState>
    );
  }
  return (
    <CheckCard
      key={row.product_id}
      row={row}
      position={index + 1}
      total={rows.length}
      onNext={() => {
        setIndex((value) => value + 1);
      }}
      onSkip={() => {
        setSkipped((value) => value + 1);
        setIndex((value) => value + 1);
      }}
    />
  );
}

function CheckCard({
  row,
  position,
  total,
  onNext,
  onSkip,
}: {
  row: ProductToCheckOut;
  position: number;
  total: number;
  onNext: () => void;
  onSkip: () => void;
}) {
  const product = useProduct(row.product_id);

  return (
    <article className="flex flex-col gap-4 px-4 py-4">
      <header className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
        <Link
          to="/products"
          className="flex w-fit items-center gap-1 hover:text-foreground"
        >
          <ArrowLeft aria-hidden="true" className="size-4" />
          Products
        </Link>
        <span className="tabular-nums">
          {position} of {total} to check
        </span>
      </header>
      {product.isPending ? (
        <ProductCheckSkeleton />
      ) : product.isError ? (
        <EmptyState
          icon={TriangleAlert}
          title="Couldn't open this product"
          description="It may have been merged, or the server is unreachable. Skip it to go on."
        >
          <Button type="button" variant="outline" onClick={onSkip}>
            Skip
          </Button>
        </EmptyState>
      ) : (
        <ProductFields
          product={product.data}
          rawText={row.raw_text}
          onNext={onNext}
          onSkip={onSkip}
        />
      )}
    </article>
  );
}

function ProductFields({
  product,
  rawText,
  onNext,
  onSkip,
}: {
  product: ProductOut;
  rawText: string;
  onNext: () => void;
  onSkip: () => void;
}) {
  const [editing, setEditing] = useState<FieldSourceKey | null>(null);
  const categories = useCategories();
  const update = useUpdateProduct(product.id);
  const confirm = useConfirmProduct(product.id);
  const busy =
    useIsMutating({ mutationKey: productKeys.detail(product.id) }) > 0;
  const error = update.error ?? confirm.error;
  const guessed = FIELDS.filter(
    ({ key }) => product.field_sources[key] === "llm",
  );

  const accept = (key: FieldSourceKey) => {
    update.reset();
    confirm.mutate({ fields: [key], expected: shownValues(product, [key]) });
  };
  const reject = (key: FieldSourceKey) => {
    const cleared = CLEARED[key];
    if (!cleared) {
      setEditing(key);
      return;
    }
    confirm.reset();
    update.mutate(cleared);
  };
  const save = (body: ProductUpdateIn) => {
    confirm.reset();
    update.mutate(body, {
      onSuccess: () => {
        setEditing(null);
      },
    });
  };
  const allCorrect = () => {
    update.reset();
    confirm.mutate({ expected: shownValues(product) }, { onSuccess: onNext });
  };
  const valueOf = (key: FieldSourceKey): string => {
    switch (key) {
      case "canonical_name":
        return product.canonical_name;
      case "brand":
        return product.brand ?? "";
      case "size":
        return sizeText(product);
      case "category":
        return (
          categories.data?.find((option) => option.value === product.category)
            ?.label ??
          product.category ??
          ""
        );
      case "tags":
        return product.tags.join(", ");
    }
  };

  return (
    <section className="flex flex-col gap-4 rounded-xl border bg-card p-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight break-words">
          {product.canonical_name}
        </h1>
        <p className="text-sm text-muted-foreground">
          Printed as <span className="font-mono">“{rawText}”</span>
        </p>
      </div>
      <ul className="divide-y rounded-lg border">
        {FIELDS.map(({ key, label }) => (
          <li
            key={key}
            aria-label={label}
            className="flex flex-col gap-2 px-3 py-2"
          >
            <div className="flex min-h-9 items-center gap-2">
              <span className="w-20 shrink-0 text-sm text-muted-foreground">
                {label}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm">
                {valueOf(key) || (
                  <span className="text-muted-foreground">Not set</span>
                )}
              </span>
              <FieldSourceMark source={product.field_sources[key]} />
              {product.field_sources[key] === "llm" && editing !== key ? (
                <span className="flex shrink-0 gap-1">
                  <Button
                    type="button"
                    size="icon"
                    variant="outline"
                    className="size-8"
                    aria-label={`Accept ${label.toLowerCase()}`}
                    disabled={busy}
                    onClick={() => {
                      accept(key);
                    }}
                  >
                    <Check aria-hidden="true" />
                  </Button>
                  <Button
                    type="button"
                    size="icon"
                    variant="outline"
                    className="size-8"
                    aria-label={`Reject ${label.toLowerCase()}`}
                    disabled={busy}
                    onClick={() => {
                      reject(key);
                    }}
                  >
                    <X aria-hidden="true" />
                  </Button>
                </span>
              ) : null}
            </div>
            {editing === key ? (
              <FieldEdit
                field={key}
                label={label}
                product={product}
                busy={busy}
                onSave={save}
                onCancel={() => {
                  setEditing(null);
                }}
              />
            ) : null}
          </li>
        ))}
      </ul>
      <p className="text-sm text-muted-foreground">
        <span className="font-medium text-foreground">
          Why these are marked:{" "}
        </span>
        {guessed.length > 0
          ? `${sentenceList(guessed.map(({ label }) => label.toLowerCase()))} ${guessed.length === 1 ? "was" : "were"} guessed by Claude from the printed text. `
          : "Nothing here is a guess. "}
        Fields from a barcode, the PLU list or you are not marked.
      </p>
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {isStale(error) ? STALE_MESSAGE : error.message}
        </p>
      ) : null}
      <div className="flex flex-col gap-2 sm:flex-row-reverse">
        <Button
          type="button"
          size="lg"
          disabled={busy || editing !== null}
          onClick={allCorrect}
        >
          All correct, next
        </Button>
        <Button
          type="button"
          size="lg"
          variant="outline"
          disabled={busy}
          onClick={onSkip}
        >
          Skip
        </Button>
      </div>
    </section>
  );
}

function sentenceList(words: string[]): string {
  const text =
    words.length < 2
      ? words.join("")
      : `${words.slice(0, -1).join(", ")} and ${words.at(-1) ?? ""}`;
  return `The ${text}`;
}

/** The name or category typed or picked over a rejected guess. */
function FieldEdit({
  field,
  label,
  product,
  busy,
  onSave,
  onCancel,
}: {
  field: FieldSourceKey;
  label: string;
  product: ProductOut;
  busy: boolean;
  onSave: (body: ProductUpdateIn) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(product.canonical_name);
  const [category, setCategory] = useState<Category | "">(
    product.category ?? "",
  );
  const categories = useCategories();
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    if (field === "canonical_name") onSave({ canonical_name: name });
    else if (category !== "") onSave({ category });
  };
  const blank =
    field === "canonical_name" ? name.trim() === "" : category === "";

  return (
    <form className="flex flex-col gap-2" onSubmit={submit}>
      {field === "canonical_name" ? (
        <Input
          aria-label={`New ${label.toLowerCase()}`}
          required
          maxLength={120}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      ) : (
        <select
          aria-label={`New ${label.toLowerCase()}`}
          value={category}
          onChange={(event) => {
            setCategory(event.target.value as Category);
          }}
          className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-base shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 md:text-sm dark:bg-input/30"
        >
          {category === "" ? (
            <option value="" disabled>
              Not set
            </option>
          ) : null}
          {(categories.data ?? []).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || blank}>
          Save
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function ProductCheckSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading products to check"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-4 w-32" />
      <Skeleton className="h-48 w-full" />
    </div>
  );
}
