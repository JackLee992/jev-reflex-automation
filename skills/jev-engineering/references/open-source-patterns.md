# Open-source patterns adopted by this skill

This reference records the source projects reviewed for the skill and the
specific engineering pattern taken from each one. It is a design map, not a
vendored dependency list. The implementations in this repository are original,
standard-library helpers; no upstream runtime is silently installed.

The review was pinned to the commits below so future updates can distinguish a
new upstream idea from a local behavior change. All six repositories declared
an MIT license at the reviewed revision.

## [`tamaratran/fast-jev-compaction`](https://github.com/tamaratran/fast-jev-compaction)

Reviewed at `e3f262a7f4d42bd8dd32ced30d26176f7cb545b0`.

Adopted patterns:

- build candidates from a tool call and its matching result rather than from
  unrelated transcript fragments;
- pin the beginning and recent tail before asking the model about older traffic;
- make retention judgments over one shared, ordered state;
- preserve retained text rather than asking a model to rewrite it;
- allow only three code-owned outcomes: keep the pair, keep the call with a
  deterministic result truncation, or remove the complete pair; and
- if request fitting, transport, validation, or policy fails, retain the input.

Local strengthening: lossy behavior requires an explicit policy version and
separate thresholds, protected facts are hard-pinned, truncation carries byte
ranges and a source hash, and retained content is redacted before it can leave
the machine. Unresolved textual errors are pinned, drop cannot use a lower
threshold than truncation, and an unverified local cache can never drive loss.
Do not copy the upstream demo's universal `0.5` threshold or moving model alias
into a calibrated workflow.

## [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast)

Reviewed at `1231850a0bf1a0c0341fe408ef1668dbbfdfac46`.

Adopted patterns:

- construct the legal action space from the current observation and give each
  executable target a code-owned identity;
- ask for the operation and each operation-specific target in one speculative
  fan-out, then consume only the target head selected by the operation;
- include current values and control state so the model can avoid toggling or
  refilling an already satisfied control;
- bind a decision to a semantic state fingerprint, consume it once before any
  mutation, and re-observe when stale;
- record execution before the next observation so a navigation failure cannot
  make an already executed action look unexecuted; and
- bound actions/model calls and stop after repeated no-change observations.

JEV still selects only bounded actions. Free text, commands, selectors, and
other generative material must come from deterministic code or a separately
validated writer, and the executor rechecks the target immediately before use.

## [`awlevin/typesafe-computer-use`](https://github.com/awlevin/typesafe-computer-use)

Reviewed at `c96dbddd04ea151c3b21cbd79c6b59c0888cc473`.

Adopted patterns:

- keep perception, decision, policy, action, and reporting as separate stages;
- merge deterministic observation sources before inference and retain stable
  provenance for every candidate;
- expose `done`, `none`, `wait`, and handoff paths so a classifier is never
  forced to click something;
- treat the minimum confidence of the action and its irreversible target as the
  diagnostic signal for that step, while ignoring unused speculative heads;
- verify text entry and other important mutations from a fresh observation;
- maintain both no-change and same-action/same-state cycle guards; and
- write per-step observation, payload, answer, timing, and final run artifacts.

The original project uses a separate writer for text and for user-facing
answers. This skill keeps the boundary stricter: JEV never generates text,
commands, or payloads, and any writer remains an independently validated tool.

## [`0xNatoshi/jev-codex-router`](https://github.com/0xNatoshi/jev-codex-router)

Reviewed at `8701ef788aa8cb0948f299538747fb01029d32b8`.

Adopted patterns:

- project a bounded decision dossier instead of sending an entire session;
- ask independent model, effort, risk-frontier, and lease questions together,
  then combine their typed answers in deterministic code;
- version routing policy and record the full distributions as diagnostics;
- reuse a route only through an explicit lease whose scope is invalidated by a
  new user turn, changed contract, tool change, error, compaction, or expiry;
- provide shadow mode, a deterministic kill switch, and a compatible fallback;
- log private identities as scoped hashes instead of persisting raw prompts; and
- open local credentials and ledgers without following symlinks, then verify
  regular-file type, current-user ownership, and private permissions.

Local strengthening: the TypeSafe credential is origin-bound to the canonical
official endpoint, redirects are refused, and localhost testing is explicit and
unauthenticated. A cache hit remains advisory instead of becoming authority.

A route lease is an optimization, not permission. This skill does not copy the
router's product-specific model taxonomy, service configuration, or fallback
economics.

## [`BYK/jev-mcp`](https://github.com/BYK/jev-mcp)

Reviewed at `cee2e6d58112d006ccd3ebad1163a31083262ae9`.

Adopted patterns:

- separate one-state prototyping, bulk mapping, and labeled evaluation;
- keep bulk results on disk and return compact summaries to the agent context;
- choose thresholds from labeled sweeps instead of intuition;
- report Brier score, ECE, coverage, confusion/failure slices, and worst misses;
- bind evaluation results to the resolved model; and
- test tool descriptions and routing behavior as data, not only as prose.

The local policy helper therefore separates a raw routing signal from the
probability of the labeled event, records decisions and outcomes independently,
refuses to treat missing labels as failures or zeros, and binds every automatic
route to a verified judgment envelope plus an exact proposed action.

## [`typesafe-ai/skills`](https://github.com/typesafe-ai/skills)

Reviewed at `65a39f393687675ce170e6094757de20370365b9`.

Adopted patterns:

- read the current TypeSafe docs and the closest cookbook before relying on a
  version-sensitive API detail;
- keep exact facts, calculations, workflow state, and execution in code;
- select from source-derived candidates instead of generating values when
  possible;
- make every question self-contained because fan-out questions cannot read one
  another's answers; and
- treat Choice/Score confidence as distribution concentration, not correctness,
  authorization, or proof that a workflow succeeded.

## How to use this map

When changing the skill, identify the upstream pattern first, then add a local
test for the invariant. Preserve the local safety strengthening unless a new
evaluation demonstrates a better policy. A new upstream commit is evidence to
review, not an automatic dependency upgrade.
