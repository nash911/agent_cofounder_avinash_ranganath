import type { ReactNode } from "react";
import type { Tone } from "../lib/config-types.js";

export interface BadgeProps {
  readonly tone?: Tone;
  readonly children: ReactNode;
}

/** A text pill. The text is the signal; `tone` only decorates it, so the
 *  meaning survives greyscale, colour-blindness and a screen reader. */
export function Badge({ tone = "neutral", children }: BadgeProps) {
  return <span className={`badge badge--${tone}`}>{children}</span>;
}
