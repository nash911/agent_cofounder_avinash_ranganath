Build the smallest maintainable application that covers every user journey detailed or implied by the product idea. Minimize unnecessary complexity, not coverage or sound internal structure, and do not add capabilities the idea does not justify.

The application in this directory is already built, styled, accessible and persistent. It renders itself from one declaration. Your primary and usually only source write is `src/app-config.ts`, which already contains a complete worked example — read it first. `AGENTS.md` documents every export you need: do not read scaffold files to discover an API, and do not rebuild forms, lists, filters, dialogs, toasts, storage or CSS that already exist.

Work autonomously in the current directory. Do not ask clarifying questions. Resolve genuine ambiguity with a sensible product decision and record that decision under `assumptions`.

Implement what the idea states or implies, and nothing else.

Required outcome:

- The application starts with `npm run dev` at exactly `http://localhost:3000`.
- It is responsive, accessible, and usable without external services or login.
- Required user data survives a page refresh.
- Where the app has mutable data or domain operations, keep UI, domain logic, and persistence behind small clear boundaries so storage or another client can be added without rewriting the UI. Do not add a backend or external API unless the idea requires one.
- Handle empty and invalid input, duplicate or repeated actions, boundary cases, malformed persisted data, and recoverable storage/runtime failures where relevant.
- Implement and run tests for every observable user journey detailed or implied by the idea. Never omit an implied journey merely to simplify the application.
- Use the included Vitest, jsdom, and Testing Library setup; keep tests in `src/**/*.test.ts` or `src/**/*.test.tsx`. Write them with `src/test/helpers.tsx`; `src/test/journeys.template.tsx` shows one worked test per journey pattern.
- Use only the dependencies already installed from the committed lockfile; do not add packages or run dependency-install commands.
- Keep concerns separated and duplication limited without unnecessary infrastructure.
- Never write a file longer than 150 lines. If a file would exceed that, split it. Prefer editing `src/app-config.ts` over creating a new file.
- Any threshold, cut-off, or vague quantity in the idea ("only a couple left", "running low", "soon", "overdue") becomes a named exported constant in `src/app-config.ts` with a one-line comment giving the chosen value, and one matching entry in `assumptions`.
- Run `npm test` once, then `npm run build` once. Repair any failure and re-run only the command that failed. Do not re-run a command that already passed.
- Never start a development server or any background process. The runner starts and verifies the server itself.
- Write `report.partial.json` at the application root using the shape described in `AGENTS.md`.
- Report `success` only when `tests_run` contains at least one user journey and every entry passed. Use `partial` when any journey failed or was not run.
- Do not write `result.json`; the challenge runner owns its audited telemetry fields.

Finish the moment `report.partial.json` is written. That file is the last thing you produce. After writing it: do not run any command, do not re-run `npm test` or `npm run build`, do not start or probe a development server, do not run `pgrep`, `ps`, `lsof`, `curl`, `wget`, `kill` or `netstat`, do not inspect files to confirm your own work, and do not write a closing summary, sign-off, or explanation of any kind. The runner starts the server and verifies it independently after you finish. Write the report and end your turn.

You may replace the starter application source when that produces a better result. Keep the included package scripts and Vitest setup so the runner can verify the finished application.
