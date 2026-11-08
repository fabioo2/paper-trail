/**
 * Controllable `IntersectionObserver` for jsdom (which has none). Components observe elements
 * as usual; a test calls `intersectAll()` to report every observed element as visible, as if
 * the user had scrolled it into view. Like the real one, observing reports the element's
 * current state once: not visible, unless `showEverything()` made the viewport tall enough to
 * show every element.
 */
const observers = new Set<FakeIntersectionObserver>();
let everythingVisible = false;

class FakeIntersectionObserver {
  readonly root = null;
  readonly rootMargin = "0px";
  readonly thresholds: readonly number[] = [0];
  private readonly targets = new Set<Element>();
  private readonly callback: IntersectionObserverCallback;

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    observers.add(this);
  }

  observe(target: Element): void {
    this.targets.add(target);
    const visible = everythingVisible;
    queueMicrotask(() => {
      if (this.targets.has(target))
        this.callback([entry(target, visible)], this);
    });
  }

  unobserve(target: Element): void {
    this.targets.delete(target);
  }

  disconnect(): void {
    this.targets.clear();
    observers.delete(this);
  }

  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }

  intersect(): void {
    if (this.targets.size === 0) return;
    this.callback(
      [...this.targets].map((target) => entry(target, true)),
      this,
    );
  }
}

function entry(target: Element, visible: boolean): IntersectionObserverEntry {
  return {
    target,
    isIntersecting: visible,
    intersectionRatio: visible ? 1 : 0,
    time: performance.now(),
    boundingClientRect: target.getBoundingClientRect(),
    intersectionRect: target.getBoundingClientRect(),
    rootBounds: null,
  } satisfies IntersectionObserverEntry;
}

export function installIntersectionObserver(): void {
  Object.defineProperty(window, "IntersectionObserver", {
    configurable: true,
    writable: true,
    value: FakeIntersectionObserver,
  });
}

/** Report every currently observed element as intersecting the viewport. */
export function intersectAll(): void {
  for (const observer of [...observers]) observer.intersect();
}

/** From now on, every element observed is reported visible straight away (a tall viewport). */
export function showEverything(): void {
  everythingVisible = true;
}

export function resetIntersectionObserver(): void {
  observers.clear();
  everythingVisible = false;
}
