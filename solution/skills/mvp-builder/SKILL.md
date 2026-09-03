---
name: mvp-builder
description: Turn a non-technical product idea into a small, tested browser application while recording assumptions.
---

# MVP Builder

1. Extract from the idea: the record, its attributes, the choice sets, the numbers, the derived states ("currently out", "running low"), the counts to display, the verbs beyond add/edit/delete, and the ambiguity.
2. Read `src/app-config.ts`, then rewrite it in one write, mapping each item to a key using the table in `AGENTS.md`.
3. Use the public journey guidance as a coverage check; omit patterns the idea does not imply and record why in `assumptions`.
4. Write journey tests in `src/journeys.test.tsx`, importing the helpers as `./test/helpers.js`; six lines per journey.
5. `npm test`, then `npm run build`. Repair, re-running only what failed.
6. Write `report.partial.json` with this exact shape, then stop.

```json
{
  "status": "success",
  "app_url": "http://localhost:3000",
  "start_command": "npm run dev",
  "summary": "Short description of the application",
  "implemented_features": ["Feature"],
  "assumptions": ["Ambiguity and the decision made"],
  "tests_run": [
    {
      "command": "npm test",
      "journey": "User-visible behaviour that was verified",
      "result": "passed"
    }
  ]
}
```

Use `success` only when `tests_run` contains at least one user journey and every entry passed. Use `partial` when useful functionality remains incomplete or any journey failed or was not run, and `failed` when the app cannot run. Never invent a passing test.
Use only `passed` or `failed` for each test result. Record an unrun check as `failed` and explain why in its journey.
