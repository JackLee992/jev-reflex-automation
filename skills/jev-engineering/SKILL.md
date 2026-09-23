---
name: jev-engineering
description: Use TypeSafe Jev inside Codex for bounded Choice, Noul, and Score judgments in development, debugging and log triage, authorized reverse engineering, UI or device automation, context retention, and calibrated decision policy. Use when the user explicitly asks for Jev or when a repeated engineering workflow genuinely needs a cheap semantic classifier or router; do not use it for ordinary coding questions that deterministic tools can answer.
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
- Versioned thresholds, decision/outcome pairing, calibration, drift, consequence
  budgets, or context retention: read
  [references/governance.md](references/governance.md).
- Emitting reusable run, event, handoff, or evaluation records: read
  [references/schemas.md](references/schemas.md).
- Extending this skill from the open-source projects named in the Jev article:
  read [references/open-source-patterns.md](references/open-source-patterns.md)
  before copying a pattern or changing a helper invariant.

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
6. **Record and improve.** Append the judgment and policy decision first, then a
   separate observed outcome linked by immutable `decision_id`. Report
   cohort-specific Brier/ECE and drift; turn disagreements, abstentions, and
   failures into golden cases before changing thresholds.

Start new integrations in **shadow mode**: compute the JEV decision and record it,
but keep the existing deterministic or human decision in control. Promote a
decision only after a representative replay set shows acceptable calibration.

Treat thresholds as versioned policy, not constants hidden in code. Bind every
gate to the pinned model, exact question/state projection, action/risk class,
evaluation evidence, and owner. Missing, stale, changed, or uncalibrated policy
forces shadow/review. Keep JEV's routing `confidence` distinct from the event
probability used for Brier/ECE; read [references/governance.md](references/governance.md)
before implementing an autonomy gate.

When reducing context, **decide what survives; do not rewrite survivors**. Only
eligible old tool call/result pairs may be dropped or deterministically
truncated. Preserve retained redacted content verbatim and pin user intent,
permissions, safety policy, checkpoints, unresolved failures, current work, and
verification evidence. Any model/policy failure retains the original context.

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
  --cache-dir .jev/cache --audit .jev/events.jsonl \
  --output .jev/judgment.json
```

The TypeSafe key may be sent only to the canonical official endpoint, with
redirects disabled. `jev_judge.py run --allow-localhost --endpoint
http://127.0.0.1:PORT/...` exists only for unauthenticated local testing: it
never loads or sends the key and its result is not trusted for autonomous
routing.

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

Map one typed question contract over JSON/JSONL items. The default is a local,
state-free preview; `--send` enables bounded concurrent calls and `--output`
writes the full redacted result atomically with mode `0600`:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" map items.jsonl questions.json \
  --output .jev/map-preview.json
python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" map items.jsonl questions.json \
  --send --output .jev/map.json --cache-dir .jev/cache --audit .jev/events.jsonl
```

Evaluate exact question variants on labeled tuning data, freeze the selected
Noul threshold, then measure it on a disjoint holdout. A holdout run refuses to
choose its own threshold:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" eval tuning.jsonl variants.json \
  --dataset-role tuning --send --output .jev/tuning.json
python3 "$JEV_SKILL_DIR/scripts/jev_batch.py" eval holdout.jsonl variants.json \
  --dataset-role holdout --threshold 0.75 --send --output .jev/holdout.json
```

`--threshold` is valid only for one Noul variant. For multiple Noul variants,
pass an exact mapping such as
`--thresholds '{"actionable_v1":0.72,"actionable_v2":0.81}'`; never reuse one
variant's cutoff for another implicitly.

Preview selection-only compaction of a JSON/JSONL trace whose tool records have
stable `id`, `kind`, `call_id`, and string `content`. Only complete, ordered
`tool_call`/`tool_result` pairs with explicit `complete: true` on both records
are eligible; live lossy behavior requires an explicit external policy version
and thresholds. Traceback/error/panic/timeout text protects the whole pair until
the same record carries structured `resolved: true` or an explicit resolved
status; prose that merely says “fixed” does not release it:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_trace_compact.py" trace.jsonl \
  --output .jev/trace-preview.json
python3 "$JEV_SKILL_DIR/scripts/jev_trace_compact.py" trace.jsonl --send \
  --policy-version trace-v1 \
  --truncate-min-confidence 0.90 --drop-min-confidence 0.95 \
  --truncate-bytes 1024 --cache-ttl-seconds 0 --audit .jev/events.jsonl \
  --output .jev/trace-compacted.json
```

The drop threshold must be greater than or equal to the truncate threshold.

Validate a versioned routing registry, record each decision before acting, and
append its later observed outcome without rewriting history:

```bash
python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" check-policy policies.json
python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" record-decision \
  policies.json route_policy .jev/judgment.json --model jev-1.13.0 \
  --proposed-action inspect_route \
  --events .jev/policy-events.jsonl
python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" record-outcome \
  .jev/policy-events.jsonl DECISION_ID --outcome-json '"inspect"'
python3 "$JEV_SKILL_DIR/scripts/jev_policy.py" report \
  policies.json route_policy .jev/policy-events.jsonl
```

The outcome must be the later observed ground-truth label and must satisfy the
question contract; this Choice example permits `"inspect"` or `"review"`.
Governance and promotion rules are in
[references/governance.md](references/governance.md); the exact validated
registry shape starts with the example below. Never invent a production
threshold from the example values above; they demonstrate CLI shape only.
[`examples/policy-shadow.example.json`](examples/policy-shadow.example.json) and
[`examples/route-request.json`](examples/route-request.json) form a validated
schema starting point, but their placeholder baseline and explicit
draft/uncalibrated/shadow status grant no authority. Only a complete
`jev_judge.py run` envelope can become auto-eligible; a bare typed answer always
stays in shadow. Auto additionally requires the canonical official service and
a live, non-cached envelope. Local cache hits may accelerate preview/evaluation,
but cannot authorize auto or lossy trace compaction. If health is required, use
`--health-events` so the helper recomputes it from the current ledger in-process.
Use `jev_judge.py run --cache-ttl-seconds 0` when producing an auto candidate.

Before the first live call involving a new data source, state briefly which
categories of data will leave the machine; this is a notice, not a cost approval
request. The helpers redact common secrets, but that is a backstop, not a
substitute for selecting the right state.

The helpers fail closed on obvious residual credentials, response/model/schema
mismatches, non-official remote endpoints, redirects, and cross-endpoint,
insecure, or expired cache entries. A moving model alias is intentionally
rejected when it resolves to a different model; calibrated workflows must
request a concrete model version.

When implementing or upgrading an integration, check the current official
[documentation index](https://docs.typesafe.ai/llms.txt), the
[HTTP API](https://docs.typesafe.ai/api.md) or selected SDK page, and the page
for every primitive used. Treat those live pages as the version-sensitive
contract; keep this skill's stricter local validation and safety policy even if
an example in a cookbook is looser.

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
- Do not use probabilistic compaction to rewrite or discard user intent,
  authorization, safety constraints, unresolved failures, or completion
  evidence. Smaller context is not success if replay semantics change.
- Reverse engineering stays within the artifact and authority the user supplied;
  JEV never expands that scope.

## Completion report

When JEV materially influenced the work, report the evidence it saw, the bounded
question, answer/probability, policy ID/version and threshold, resulting action,
independent verification, and whether an outcome label is still pending.
Separate observed facts from JEV inferences.
