import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync } from "node:fs";

/**
 * Berget (and any other OpenAI-compatible vLLM deployment) needs
 * `chat_template_kwargs.enable_thinking` on the wire to keep reasoning off. Pi
 * emits nothing at all for `--thinking off` unless the model definition carries
 * `thinkingFormat: "chat-template"`, so a provider registered by the organizers
 * under a name we cannot predict would silently reason on every turn. Thinking
 * is the largest measured cost lever in this challenge, so the guard defaults to
 * disabling it and never overwrites a field the provider layer already set.
 *
 * The guard reads `ctx.thinkingLevel` — never `PI_REASONING_LEVEL`, which is
 * absent from Pi's own process environment and would invert the decision. It
 * also fires only for `api: "openai-completions"` payloads: a top-level
 * `chat_template_kwargs` on an Anthropic Messages or Google Generative AI body
 * is an unknown field that would fail the request outright.
 */

/** Hosts whose first-party APIs already serialize thinking correctly. */
export const FIRST_PARTY_HOSTS: readonly string[] = [
  "api.z.ai",
  "open.bigmodel.cn",
  "api.openai.com",
  "api.anthropic.com",
  "api.x.ai",
  "api.deepseek.com",
  "api.moonshot.cn",
  "api.moonshot.ai",
  "openrouter.ai",
  "api.together.xyz",
  "generativelanguage.googleapis.com",
];

/**
 * `chat_template_kwargs` is a vLLM extension of the OpenAI *chat completions*
 * request body. An `anthropic-messages`, `google-generative-ai` or
 * `openai-responses` endpoint rejects the unknown top-level key outright, so the
 * guard only ever fires for this API type. `undefined` keeps the old behaviour
 * for a context shape that carries no API at all (unit stubs); any other value
 * stands the guard down.
 */
export const GUARDED_API = "openai-completions";

/** Payload keys that mean the provider layer already decided about thinking. */
export const THINKING_PAYLOAD_KEYS: readonly string[] = [
  "chat_template_kwargs",
  "thinking",
  "enable_thinking",
  "reasoning",
  "reasoning_effort",
];

/** The slice of Pi's `ExtensionContext` the decision actually depends on. */
export interface ThinkingGuardContext {
  readonly model?:
    | { readonly baseUrl?: string | undefined; readonly api?: string | undefined }
    | undefined;
  readonly thinkingLevel?: string | undefined;
}

/** True when the request body is an OpenAI chat-completions one, or unknown. */
export function isGuardedApi(api: string | undefined): boolean {
  return api === undefined || api === GUARDED_API;
}

/** Why the guard fired or stood down, for the stderr audit line. */
export interface ThinkingGuardDecision {
  readonly fired: boolean;
  readonly reason: string;
  readonly level: string;
  readonly enableThinking: boolean;
  readonly baseUrl: string;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hostnameOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return baseUrl.toLowerCase();
  }
}

/** True when the base URL points at a vendor endpoint Pi already serializes for. */
export function isFirstPartyHost(baseUrl: string | undefined): boolean {
  if (!baseUrl) return false;
  const hostname = hostnameOf(baseUrl);
  return FIRST_PARTY_HOSTS.some((host) => hostname === host || hostname.endsWith(`.${host}`));
}

/** Resolve the effective thinking level, defaulting to `off` in every ambiguous case. */
export function resolveThinkingLevel(
  context: ThinkingGuardContext,
  environment: NodeJS.ProcessEnv,
): string {
  const configured = context.thinkingLevel ?? environment.CHALLENGE_THINKING ?? "off";
  const trimmed = configured.trim();
  return trimmed === "" ? "off" : trimmed;
}

/** The pure decision, exported so it can be unit-tested without running Pi. */
export function decideThinkingGuard(
  payload: unknown,
  context: ThinkingGuardContext,
  environment: NodeJS.ProcessEnv,
): ThinkingGuardDecision {
  const baseUrl = context.model?.baseUrl ?? "";
  const level = resolveThinkingLevel(context, environment);
  const stand = (reason: string): ThinkingGuardDecision => ({
    fired: false,
    reason,
    level,
    enableThinking: false,
    baseUrl,
  });

  if (environment.HARNESS_THINKING_GUARD === "0") return stand("disabled-by-env");
  if (!isPlainObject(payload)) return stand("payload-not-an-object");
  const api = context.model?.api;
  if (!isGuardedApi(api)) return stand(`api-is-${api}`);
  if (isFirstPartyHost(baseUrl)) return stand("first-party-host");
  const existing = THINKING_PAYLOAD_KEYS.find((key) => key in payload);
  if (existing !== undefined) return stand(`payload-has-${existing}`);

  return { fired: true, reason: "applied", level, enableThinking: level !== "off", baseUrl };
}

/**
 * Returns a replacement payload, or `undefined` to leave the request untouched.
 * The input payload is never mutated in place.
 */
export function guardPayload(
  payload: unknown,
  context: ThinkingGuardContext,
  environment: NodeJS.ProcessEnv = process.env,
): Record<string, unknown> | undefined {
  const decision = decideThinkingGuard(payload, context, environment);
  if (!decision.fired || !isPlainObject(payload)) return undefined;
  return { ...payload, chat_template_kwargs: { enable_thinking: decision.enableThinking } };
}

/**
 * The value the provider will actually receive as
 * `chat_template_kwargs.enable_thinking`, or `null` when the request carries no
 * such field. Read from the *final* payload so the audit log records the wire,
 * not the guard's intention: with `.pi-agent/models.json` loaded Pi sets the
 * field itself and the guard stands down, and the log must still show `false`.
 */
export function wireEnableThinking(payload: unknown): boolean | null {
  if (!isPlainObject(payload)) return null;
  const kwargs = payload.chat_template_kwargs;
  if (!isPlainObject(kwargs)) return null;
  const value = kwargs.enable_thinking;
  return typeof value === "boolean" ? value : null;
}

/** The payload the guard hands back when it fires; never mutates the input. */
export function applyDecision(
  payload: Record<string, unknown>,
  decision: ThinkingGuardDecision,
): Record<string, unknown> {
  return { ...payload, chat_template_kwargs: { enable_thinking: decision.enableThinking } };
}

function appendPayloadLog(
  decision: ThinkingGuardDecision,
  modelId: string,
  wire: boolean | null,
): void {
  const logPath = process.env.HARNESS_PAYLOAD_LOG;
  if (!logPath) return;
  try {
    const record = {
      ts: new Date().toISOString(),
      enable_thinking: wire,
      source: decision.fired ? "guard" : wire === null ? "none" : "provider",
      fired: decision.fired,
      reason: decision.reason,
      level: decision.level,
      model: modelId,
    };
    appendFileSync(logPath, `${JSON.stringify(record)}\n`, "utf8");
  } catch {
    // A missing or unwritable log must never fail a judged request.
  }
}

export default function thinkingGuard(pi: ExtensionAPI): void {
  pi.on("before_provider_request", (event, context) => {
    const decision = decideThinkingGuard(event.payload, context, process.env);
    const modelId = context.model?.id ?? "unknown";
    const replacement =
      decision.fired && isPlainObject(event.payload)
        ? applyDecision(event.payload, decision)
        : undefined;
    const wire = wireEnableThinking(replacement ?? event.payload);
    console.error(
      `[thinking-guard] fired=${decision.fired} reason=${decision.reason} ` +
        `level=${decision.level} wire_enable_thinking=${String(wire)} model=${modelId} ` +
        `api=${context.model?.api ?? "unknown"}`,
    );
    appendPayloadLog(decision, modelId, wire);
    return replacement;
  });
}
