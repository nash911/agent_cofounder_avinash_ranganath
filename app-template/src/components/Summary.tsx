import { useId } from "react";
import { computeStats } from "../lib/collection.js";
import type { AnyConfig, Row } from "../lib/config-types.js";

export interface SummaryProps {
  readonly config: AnyConfig;
  readonly rows: readonly Row[];
}

/** Stat tiles. Each value is named by its own label through `aria-labelledby`,
 *  so `getByLabelText("Running low")` returns the element holding the number. */
export function Summary({ config, rows }: SummaryProps) {
  const base = useId();
  const stats = computeStats(config, rows);
  if (stats.length === 0) return null;

  return (
    <section className="stats" role="status" aria-live="polite" aria-label="Summary">
      {stats.map((entry) => {
        const labelId = `${base}${entry.id}`;
        return (
          <div key={entry.id} className={entry.emphasis ? "stat stat--emphasis" : "stat"}>
            <span className="stat__label" id={labelId}>
              {entry.label}
            </span>
            <span className="stat__value" aria-labelledby={labelId}>
              {entry.value}
            </span>
          </div>
        );
      })}
    </section>
  );
}
