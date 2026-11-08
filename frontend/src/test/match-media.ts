/**
 * Controllable `window.matchMedia` for jsdom (which has none).
 * Only `(prefers-color-scheme: dark)` is meaningful; every other query never matches.
 */
const DARK_QUERY = "(prefers-color-scheme: dark)";

type ChangeListener = (event: MediaQueryListEvent) => void;

let prefersDark = false;
const lists = new Set<FakeMediaQueryList>();

class FakeMediaQueryList extends EventTarget {
  readonly media: string;
  onchange: ChangeListener | null = null;
  private readonly legacyListeners = new Set<ChangeListener>();

  constructor(media: string) {
    super();
    this.media = media;
  }

  get matches(): boolean {
    return this.media === DARK_QUERY && prefersDark;
  }

  addListener(listener: ChangeListener | null): void {
    if (listener) this.legacyListeners.add(listener);
  }

  removeListener(listener: ChangeListener | null): void {
    if (listener) this.legacyListeners.delete(listener);
  }

  notify(): void {
    const event = new Event("change") as MediaQueryListEvent;
    Object.defineProperties(event, {
      matches: { value: this.matches },
      media: { value: this.media },
    });
    this.dispatchEvent(event);
    this.onchange?.(event);
    for (const listener of this.legacyListeners) listener(event);
  }
}

export function installMatchMedia(): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList => {
      const list = new FakeMediaQueryList(query);
      lists.add(list);
      // Structurally a MediaQueryList for everything the app uses.
      return list;
    },
  });
}

/** Change the OS colour-scheme preference and notify live listeners. */
export function setPrefersDark(value: boolean): void {
  prefersDark = value;
  for (const list of lists) {
    if (list.media === DARK_QUERY) list.notify();
  }
}

export function resetMatchMedia(): void {
  prefersDark = false;
  lists.clear();
}
