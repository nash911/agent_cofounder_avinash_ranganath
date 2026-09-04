/** Whole-day arithmetic on `yyyy-mm-dd` strings — the shape every `date` field
 *  stores. Date-relative rules ("more than N days ago", "within the next N
 *  days") are declared with these instead of being hand-rolled, so every
 *  configuration counts days the same way.
 *
 *  Days are counted between calendar dates, never between instants: the clock
 *  time, the timezone offset and daylight saving never move a result. An unset
 *  ("") or malformed date is not an error — it yields 0, so a predicate over a
 *  record nobody has dated yet is simply false rather than a crash. */

const DAY_MS = 86_400_000;

/** Calendar date as a UTC midnight stamp, or `undefined` if it is not a real
 *  `yyyy-mm-dd` date. The round-trip check rejects 2025-02-30, which `Date.UTC`
 *  would otherwise roll forward into March. */
function stampOf(iso: string): number | undefined {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (parts === null) return undefined;
  const year = Number(parts[1]);
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  const stamp = Date.UTC(year, month - 1, day);
  const back = new Date(stamp);
  const same = back.getUTCFullYear() === year
    && back.getUTCMonth() === month - 1
    && back.getUTCDate() === day;
  return same ? stamp : undefined;
}

/** Today as `yyyy-mm-dd`, the UTC calendar date — the same date a `date`
 *  field's `initial: "today"` stores and the same one a test's
 *  `new Date(...).toISOString().slice(0, 10)` produces, so a rule, a stored
 *  value and an assertion never disagree about which day it is, whatever the
 *  clock says locally. */
export function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/** True when `iso` is a real `yyyy-mm-dd` calendar date. "" is false. */
export function isValidDate(iso: string): boolean {
  return stampOf(iso) !== undefined;
}

/** Whole days from `a` to `b`: positive when `b` is later. 0 if either is
 *  unset or malformed. */
export function daysBetween(a: string, b: string): number {
  const from = stampOf(a);
  const to = stampOf(b);
  if (from === undefined || to === undefined) return 0;
  return Math.round((to - from) / DAY_MS);
}

/** Days from today to `iso`: positive in the future ("due in 3 days"),
 *  negative in the past. 0 if unset or malformed. */
export function daysUntil(iso: string): number {
  return daysBetween(today(), iso);
}

/** Days from `iso` to today: positive in the past ("last seen 9 days ago"),
 *  negative in the future. 0 if unset or malformed. */
export function daysSince(iso: string): number {
  return daysBetween(iso, today());
}
