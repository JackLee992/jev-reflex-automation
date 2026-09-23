---
name: jev-engineering
description: Use TypeSafe Jev inside Codex for bounded Choice, Noul, and Score judgments in development, debugging and log triage, authorized reverse engineering, and UI or device automation. Use when the user explicitly asks for Jev or when a repeated engineering workflow genuinely needs a cheap semantic classifier or router; do not use it for ordinary coding questions that deterministic tools can answer.
---

# JEV Engineering

Use JEV as a narrow judgment primitive inside a code-owned workflow. JEV chooses,
scores, or estimates a yes/no probability; Codex and deterministic tools still
collect evidence, calculate, mutate state, and verify outcomes.

## Route the task

Read one primary domain reference. Add `judgment-design.md` when defining
questions or thresholds, and `schemas.md` when persisting or handing off a run;
do not load unrelated references:

- Development, review, task routing, or completion evidence: read
  [references/development.md](references/development.md).
- Debugging or log analysis: read
  [references/debugging-logs.md](references/debugging-logs.md).
- Authorized static or dynamic reverse engineering: read
  [references/reverse-engineering.md](references/reverse-engineering.md).
- Browser, desktop, mobile, device, or workflow automation: read
  [references/automation.md](references/automation.md).
- Designing questions, thresholds, or golden sets: read
  [references/judgment-design.md](references/judgment-design.md).
- Emitting reusable run, event, handoff, or evaluation records: read
  [references/schemas.md](references/schemas.md).

## Required workflow

1. **Extract facts locally.** Use tests, parsers, debuggers, accessibility trees,
   protocol traces, symbol tools, or targeted searches. Do not ask JEV for a fact
   that code can calculate or a tool can observe.
2. **Minimize and redact.** Send only the fields needed by the questions. Never
   send credentials, cookies, private keys, authorization headers, secure-field
   values, or an unfiltered repository/log dump.
3. **Define bounded judgments.** Build legal options in code, include an explicit
   abstention/handoff option when appropriate, and make every question literally
   self-contained. Fan out independent questions in one call.
4. **Apply policy in code.** Probabilities may route or prioritize work; they do
   not prove facts, authorize destructive actions, or verify that an action
   succeeded. Hard safety rules and permission boundaries always win.
5. **Act through the normal tool.** Prefer idempotent operations and stable
   object references. Re-observe after every mutation and check a concrete
   postcondition.
6. **Record and improve.** Log model version, request hash, answers, thresholds,
   selected action, evidence, and observed outcome. Turn disagreements,
   abstentions, and failures into golden cases before changing thresholds.

Start new integrations in **shadow mode**: compute the JEV decision and record it,
but keep the existing deterministic or human decision in control. Promote a
decision only after a representative replay set shows acceptable calibration.

If no API key is available, prepare and validate the redacted request, then use
the deterministic fallback. Lack of JEV must not break an otherwise safe
workflow.

For this project, JEV cost is not an approval checkpoint. After minimizing and
redacting the state, call JEV directly whenever a bounded judgment is useful;
do not ask for confirmation solely because the call has a price. Data exposure,
user authorization, and action risk remain independent gates.

## Local helpers

Set `JEV_SKILL_DIR` to the directory containing this `SKILL.md`; helper paths are
relative to the skill, never to the user's current repository. For a standard
Codex installation:

```bash
JEV_SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/jev-engineering"
```

Validate and preview a request without sending it:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_judge.py" check request.json
```

Send a validated request, with an endpoint/model/version-scoped 24-hour cache
and replayable append-only judgment audit:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_judge.py" run request.json \
  --cache-dir .jev/cache --audit .jev/events.jsonl
```

Build a compact log-triage request locally. It always preserves exact byte
references; it preserves original line numbers within a bounded local prefix
scan and marks when only slice-relative lines are available. It also marks
truncated tails and offers bounded read-only diagnostic candidates. Add `--send`
only after checking the preview and data boundary:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_log_triage.py" app.log --goal "find the first actionable cause"
python3 "$JEV_SKILL_DIR/scripts/jev_log_triage.py" app.log --goal "find the first actionable cause" \
  --send --cache-dir .jev/cache --audit .jev/events.jsonl
```

Before the first live call involving a new data source, state briefly which
categories of data will leave the machine; this is a notice, not a cost approval
request. The helpers redact common secrets, but that is a backstop, not a
substitute for selecting the right state.

The helpers fail closed on obvious residual credentials, response/model/schema
mismatches, insecure remote endpoints, and cross-endpoint or expired cache
entries. A moving model alias is intentionally rejected when it resolves to a
different model; calibrated workflows must request a concrete model version.

## Non-negotiable boundaries

- Do not use JEV to generate code, commands, payloads, exploit steps, or user
  content. Codex creates those through the normal workflow.
- Do not let the same probabilistic answer both propose a high-impact action and
  authorize it. Payments, publishing, messaging, deletion, permission grants,
  credential use, and irreversible changes require deterministic policy and the
  user's authorization.
- Do not treat `confidence` as correctness. Thresholds belong to a pinned model,
  a task family, and a dated evaluation set.
- Do not silently switch from a pinned model to a moving alias in a calibrated
  workflow.
- Reverse engineering stays within the artifact and authority the user supplied;
  JEV never expands that scope.

## Completion report

When JEV materially influenced the work, report the evidence it saw, the bounded
question, answer/probability, policy threshold, resulting action, and independent
verification. Separate observed facts from JEV inferences.
