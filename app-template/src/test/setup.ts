import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library only auto-registers its cleanup when `afterEach` is a
// global, which this config did not provide; without it every rendered
// tree leaks into the next test and `getBy*` queries start failing with
// "multiple elements found". Register it explicitly so each test starts
// from an empty document.
afterEach(cleanup);

// Browser storage outlives cleanup, so saved rows would otherwise leak into
// the next test and make the file order-dependent. Either accessor can throw
// in a restricted context, so each clear is guarded on its own and a failure
// never fails the test.
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // storage unavailable; nothing to clear
  }
  try {
    sessionStorage.clear();
  } catch {
    // storage unavailable; nothing to clear
  }
});
