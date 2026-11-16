import { act, fireEvent, screen, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  type Mock,
  vi,
} from "vitest";

import {
  RECEIPT_POLL_INTERVAL_MS,
  type ReceiptStatus,
} from "@/features/receipts/api";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import {
  cad,
  receiptDetail,
  type ReceiptDetailOut,
} from "@/test/fixtures/receipts";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const PREVIEW_URL = "blob:receipt-preview";
const THREE_MINUTES_MS = 3 * 60_000;

const processing = receiptDetail({
  status: "processing",
  stage: "reading",
  store_name: "",
  purchased_on: null,
  total: null,
  item_count: 0,
  items: [],
});

function photo(): File {
  return new File(["receipt-bytes"], "receipt.jpg", { type: "image/jpeg" });
}

function problem(status: number, title: string): Response {
  const body: ProblemDetail = {
    type: "about:blank",
    title,
    status,
    detail: null,
    instance: "/api/receipts",
    errors: null,
  };
  return HttpResponse.json(body, {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

interface Upload {
  name: string;
  type: string;
  bytes: string;
}

/** POST /api/receipts answers with `respond`; the `file` part of each upload is returned. */
function serveUpload(respond: () => Response | Promise<Response>): Upload[] {
  const uploads: Upload[] = [];
  server.use(
    http.post("/api/receipts", async ({ request }) => {
      const file = (await request.formData()).get("file");
      if (!(file instanceof File)) throw new Error("upload has no file part");
      uploads.push({
        name: file.name,
        type: file.type,
        bytes: await file.text(),
      });
      return respond();
    }),
  );
  return uploads;
}

function accepted(id = 7): Response {
  return HttpResponse.json(
    { id, status: "processing", stage: "queued" },
    { status: 202 },
  );
}

/** GET /api/receipts/{id} walks through `states`, repeating the last; returns the polled paths. */
function serveProgress(...states: ReceiptDetailOut[]): string[] {
  const polls: string[] = [];
  server.use(
    http.get("/api/receipts/:receiptId", ({ request }) => {
      polls.push(new URL(request.url).pathname);
      const state = states[Math.min(polls.length, states.length) - 1];
      return HttpResponse.json(state);
    }),
  );
  return polls;
}

/** GET /api/receipts/{id} answers with each responder in turn, repeating the last. */
function servePolls(...responders: (() => Response)[]): string[] {
  const polls: string[] = [];
  server.use(
    http.get("/api/receipts/:receiptId", ({ request }) => {
      polls.push(new URL(request.url).pathname);
      const respond = responders[Math.min(polls.length, responders.length) - 1];
      if (!respond) throw new Error("no poll responder");
      return respond();
    }),
  );
  return polls;
}

async function advance(ms: number): Promise<void> {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}

async function heading(): Promise<HTMLElement> {
  const main = await screen.findByRole("main");
  const [first] = await within(main).findAllByRole("heading", { level: 1 });
  if (!first) throw new Error("page has no heading");
  return first;
}

function sendButton(): HTMLElement {
  return screen.getByRole("button", { name: /send|uploading/i });
}

async function pickPhoto(user: UserEvent, label = /take a photo/i) {
  renderApp("/scan");
  await user.upload(await screen.findByLabelText(label), photo());
  await screen.findByRole("img");
}

async function pickAndSend(user: UserEvent) {
  await pickPhoto(user);
  await user.click(sendButton());
}

describe("Scan page", () => {
  let user: UserEvent;
  let createObjectURL: Mock<(blob: Blob) => string>;
  let revokeObjectURL: Mock<(url: string) => void>;

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    createObjectURL = vi.fn(() => PREVIEW_URL);
    revokeObjectURL = vi.fn();
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("walks from picking to reading with the exact copy", async () => {
    serveUpload(accepted);
    serveProgress(processing);

    renderApp("/scan");

    expect(textOf(await heading())).toBe("Scan a receipt");
    const take = screen.getByLabelText("Take a photo");
    expect(screen.getByLabelText("Choose from library")).toBeInTheDocument();

    await user.upload(take, photo());
    expect(textOf(await heading())).toBe("Send this photo?");
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Pick a different photo" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(textOf(await heading())).toBe(
      "Reading your receipt — about 20 seconds",
    );
  });

  it("opens the camera from Take a photo and the library from Choose from library", async () => {
    renderApp("/scan");

    const take = await screen.findByLabelText(/take a photo/i);
    const library = screen.getByLabelText(/choose from library/i);

    expect(take).toHaveAttribute("type", "file");
    expect(take).toHaveAttribute("accept", "image/*");
    expect(take).toHaveAttribute("capture", "environment");
    expect(library).toHaveAttribute("accept", "image/*");
    expect(library).not.toHaveAttribute("capture");
  });

  it.each([/take a photo/i, /choose from library/i])(
    "previews a photo picked with %s before sending anything",
    async (label) => {
      const uploads = serveUpload(accepted);

      await pickPhoto(user, label);

      expect(screen.getByRole("img")).toHaveAttribute("src", PREVIEW_URL);
      expect(createObjectURL).toHaveBeenCalledWith(
        expect.objectContaining({ name: "receipt.jpg" }),
      );
      expect(uploads).toEqual([]);
    },
  );

  it("discarding the photo returns to the picker and frees the preview", async () => {
    await pickPhoto(user);

    await user.click(
      screen.getByRole("button", { name: /pick a different photo/i }),
    );

    expect(await screen.findByLabelText(/take a photo/i)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledWith(PREVIEW_URL);
  });

  it("shows Uploading while the photo is sent, then frees the preview", async () => {
    let release: (() => void) | undefined;
    const uploads = serveUpload(
      () =>
        new Promise<Response>((resolve) => {
          release = () => {
            resolve(accepted());
          };
        }),
    );
    serveProgress(processing);

    await pickAndSend(user);

    const button = await screen.findByRole("button", { name: /uploading/i });
    expect(button).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /pick a different photo/i }),
    ).toBeDisabled();
    expect(uploads).toEqual([
      { name: "receipt.jpg", type: "image/jpeg", bytes: "receipt-bytes" },
    ]);

    release?.();

    expect(textOf(await heading())).toMatch(/reading/i);
    expect(revokeObjectURL).toHaveBeenCalledWith(PREVIEW_URL);
  });

  it("a double tap on Send uploads once", async () => {
    const uploads = serveUpload(async () => {
      await delay(500);
      return accepted();
    });
    serveProgress(processing);

    await pickPhoto(user);
    const send = sendButton();
    // Two taps inside one tick: before the mutation's pending state has re-rendered.
    fireEvent.click(send);
    fireEvent.click(send);
    await advance(1_000);

    expect(textOf(await heading())).toMatch(/reading/i);
    expect(uploads).toHaveLength(1);
  });

  it("polls every 2 seconds and shows the receipt once it is read", async () => {
    serveUpload(accepted);
    const polls = serveProgress(processing, processing, receiptDetail());

    await pickAndSend(user);
    expect(textOf(await heading())).toMatch(/reading/i);
    expect(polls).toHaveLength(1);

    await advance(RECEIPT_POLL_INTERVAL_MS);
    expect(polls).toHaveLength(2);
    expect(textOf(await heading())).toMatch(/reading/i);

    await advance(RECEIPT_POLL_INTERVAL_MS);
    expect(textOf(await heading())).toMatch(/saved/i);

    const card = textOf(screen.getByRole("main"));
    expect(card).toContain("COSTCO WHOLESALE #1234");
    expect(card).toContain(formatDate("2026-11-01"));
    expect(card).toMatch(/items\s*2/i);
    expect(card).toContain(formatMoney(cad(1155)));
    expect(screen.getByRole("link", { name: /view receipt/i })).toHaveAttribute(
      "href",
      "/receipts/7",
    );
    expect(polls.every((path) => path === "/api/receipts/7")).toBe(true);
  });

  it.each<{ status: ReceiptStatus; title: RegExp; link: RegExp | null }>([
    { status: "completed", title: /saved/i, link: /view receipt/i },
    {
      status: "needs_review",
      title: /second look/i,
      link: /review receipt/i,
    },
    { status: "failed", title: /couldn['’]t read/i, link: null },
  ])(
    "stops polling once the receipt is $status",
    async ({ status, title, link }) => {
      serveUpload(accepted);
      const polls = serveProgress(processing, receiptDetail({ status }));

      await pickAndSend(user);
      await advance(RECEIPT_POLL_INTERVAL_MS);
      expect(textOf(await heading())).toMatch(title);
      const settledPolls = polls.length;

      await advance(RECEIPT_POLL_INTERVAL_MS * 5);

      expect(polls).toHaveLength(settledPolls);
      if (link) {
        expect(screen.getByRole("link", { name: link })).toHaveAttribute(
          "href",
          "/receipts/7",
        );
      } else {
        expect(within(screen.getByRole("main")).queryByRole("link")).toBeNull();
      }
    },
  );

  it("Try again after a failed read goes back to the picker", async () => {
    serveUpload(accepted);
    serveProgress(receiptDetail({ status: "failed", stage: "failed" }));

    await pickAndSend(user);
    await user.click(await screen.findByRole("button", { name: /try again/i }));

    expect(await screen.findByLabelText(/take a photo/i)).toBeInTheDocument();
  });

  it("a photo sent before links to its existing receipt without polling", async () => {
    serveUpload(() =>
      HttpResponse.json(
        { id: 42, status: "completed", stage: "done" },
        { status: 200 },
      ),
    );
    const polls = serveProgress(receiptDetail({ id: 42 }));

    await pickAndSend(user);

    expect(textOf(await heading())).toMatch(/already scanned/i);
    expect(screen.getByRole("link", { name: /open receipt/i })).toHaveAttribute(
      "href",
      "/receipts/42",
    );
    await advance(RECEIPT_POLL_INTERVAL_MS * 3);
    expect(polls).toEqual([]);
  });

  it.each([
    { status: 413, title: "Payload Too Large", toast: /too large/i },
    { status: 415, title: "Unsupported Media Type", toast: /can['’]t read/i },
    { status: 500, title: "Internal Server Error", toast: /couldn['’]t send/i },
  ])(
    "a $status keeps the photo and explains why in a toast",
    async ({ status, title, toast }) => {
      serveUpload(() => problem(status, title));

      await pickAndSend(user);

      expect(await screen.findByText(toast)).toBeInTheDocument();
      expect(screen.getByRole("img")).toHaveAttribute("src", PREVIEW_URL);
      expect(sendButton()).toBeEnabled();
    },
  );

  it("a network failure keeps the photo so Send can retry it", async () => {
    let online = false;
    const uploads = serveUpload(() =>
      online ? accepted() : HttpResponse.error(),
    );
    serveProgress(processing);

    await pickAndSend(user);

    expect(
      await screen.findByText(/check your connection/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("img")).toHaveAttribute("src", PREVIEW_URL);

    online = true;
    await user.click(sendButton());

    expect(textOf(await heading())).toMatch(/reading/i);
    expect(uploads).toHaveLength(2);
  });

  it("a receipt that is gone stops polling and points to Receipts", async () => {
    serveUpload(accepted);
    const polls = servePolls(() => problem(404, "Not Found"));

    await pickAndSend(user);

    expect(textOf(await heading())).toMatch(/couldn['’]t find/i);
    expect(
      screen.getByRole("link", { name: /go to receipts/i }),
    ).toHaveAttribute("href", "/receipts");
    await advance(RECEIPT_POLL_INTERVAL_MS * 5);
    expect(polls).toHaveLength(1);
  });

  it("rides out two failed polls, stops on the third, and Try again resumes", async () => {
    serveUpload(accepted);
    const polls = servePolls(
      () => HttpResponse.json(processing),
      () => problem(500, "Internal Server Error"),
      () => HttpResponse.error(),
      () => HttpResponse.json(processing),
      () => problem(503, "Service Unavailable"),
      () => HttpResponse.error(),
      () => problem(500, "Internal Server Error"),
      () => HttpResponse.json(processing),
      () => HttpResponse.json(receiptDetail()),
    );

    await pickAndSend(user);
    await advance(RECEIPT_POLL_INTERVAL_MS * 3);
    expect(polls).toHaveLength(4);
    expect(textOf(await heading())).toMatch(/reading/i);

    await advance(RECEIPT_POLL_INTERVAL_MS * 3);
    expect(polls).toHaveLength(7);
    expect(textOf(await heading())).toMatch(/lost connection/i);
    await advance(RECEIPT_POLL_INTERVAL_MS * 5);
    expect(polls).toHaveLength(7);

    await user.click(screen.getByRole("button", { name: /try again/i }));
    expect(textOf(await heading())).toMatch(/reading/i);
    await advance(RECEIPT_POLL_INTERVAL_MS);

    expect(textOf(await heading())).toMatch(/saved/i);
    expect(polls).toHaveLength(9);
  });

  it("gives up after 3 minutes and points to Receipts", async () => {
    serveUpload(accepted);
    const polls = serveProgress(processing);

    await pickAndSend(user);
    await advance(THREE_MINUTES_MS);

    expect(textOf(await heading())).toMatch(/still working/i);
    expect(
      screen.getByRole("link", { name: /go to receipts/i }),
    ).toHaveAttribute("href", "/receipts");
    const pollsAtTimeout = polls.length;
    expect(pollsAtTimeout).toBeGreaterThan(80);

    await advance(RECEIPT_POLL_INTERVAL_MS * 5);
    expect(polls).toHaveLength(pollsAtTimeout);
  });

  it("leaving the page mid-poll stops polling", async () => {
    serveUpload(accepted);
    const polls = serveProgress(processing);

    const { router } = renderApp("/scan");
    await user.upload(await screen.findByLabelText(/take a photo/i), photo());
    await user.click(await screen.findByRole("button", { name: /send/i }));
    expect(textOf(await heading())).toMatch(/reading/i);

    await act(() => router.navigate({ to: "/receipts" }));
    await screen.findByRole("heading", { name: /no receipts yet/i });
    const pollsAtLeave = polls.length;

    await advance(RECEIPT_POLL_INTERVAL_MS * 5);

    expect(polls).toHaveLength(pollsAtLeave);
  });
});
