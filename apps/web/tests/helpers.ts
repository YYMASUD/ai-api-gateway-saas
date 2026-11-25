/** Shared test utilities. This file is not a test suite. */

/** A promise whose settlement the test controls, for deferred-response mocks. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Drain pending microtasks so stale async continuations run before assertions. */
export function flushAsync(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** Every value currently held in session/local storage, for secret-leak scans. */
export function allStorageValues(): string[] {
  const values: string[] = [];
  for (const storage of [window.sessionStorage, window.localStorage]) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key !== null) values.push(storage.getItem(key) ?? "");
    }
  }
  return values;
}
