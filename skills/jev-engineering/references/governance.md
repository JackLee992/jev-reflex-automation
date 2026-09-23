# JEV policy, outcomes, calibration, and context retention

Typed answers are an interface, not a production policy. A dependable workflow
keeps three records separate:

1. the raw JEV judgment and full distribution;
2. the deterministic policy decision made from that judgment; and
3. the later observed outcome, which may arrive minutes or weeks later.

Never rewrite an earlier decision when the outcome arrives. Append a second
record linked by an immutable `decision_id` so calibration can be reproduced.

## Treat thresholds as versioned policy

A threshold must belong to one exact cohort. At minimum bind it to:

- policy ID and version;
- pinned requested and resolved model;
- canonical service identity and endpoint;
- exact question ID, type, instructions/criteria digest, and a hash-covered
  state-projection version;
- action type and risk class;
- shadow/review/auto mode and allowed autonomous actions;
- routing signal and its review/auto bands;
- labeled evaluation set, minimum sample count, metric limits, and report digest;
- policy owner, approval time, expiry or review time; and
- wrong-action consequence estimate and human-escalation cost, when known.

If any binding changes, create a new policy version and return to shadow mode.
Do not silently reuse thresholds across models, prompts, state projections,
actions, customers, languages, or risk classes. If a registry entry is absent,
expired, uncalibrated, or malformed, the safe result is `shadow`, `review`, or
`abstain`—never autonomous action.

Hard permission rules remain outside the registry. A threshold cannot authorize
payment, deletion, deployment, publishing, external messaging, credential use,
access changes, or an expansion of the user's scope.

An automatic route also requires a successful, live `jev_judge run` envelope,
not a detached answer object or local cache replay. Verify its judgment ID,
request and question-contract hashes, requested/resolved model, exact question
ID, state-projection version, canonical official endpoint, timestamp, and
`cached: false` status before consulting thresholds. A caller must name the
proposed action, and that exact action must be present in
the policy's autonomous-action allowlist. High-risk or high-impact action types
never become automatic through this helper. Consume each trusted judgment at
most once per decision ledger so replay cannot trigger a second action.

Treat the local cache as an unverified performance layer. Open cache files
without following symlinks and require private current-user regular files, but
do not let file permissions masquerade as cryptographic provenance: cache hits
may support preview and evaluation, never automatic action or lossy compaction.

## Separate routing confidence from calibration probability

For `Choice` and `Score`, JEV's `confidence` describes concentration of the
returned distribution. It is useful as a routing signal, but it is not the
probability that the workflow is correct. `Noul` has no separate confidence.

For calibration, record a probability tied to the labeled event:

- `Choice`: normally `probabilities[selected_choice]` for the event “the chosen
  option is correct”;
- `Noul`: `noul` when the predicted label is true, or `1 - noul` when the
  predicted label is false;
- `Score`: use the probability of an explicitly labeled level, or a declared
  binary acceptance event. Do not apply a binary Brier score to the scalar
  expected score or to `confidence`.

Store both values and both bases. Missing calibration probability is `missing`,
not zero. Do not include it in Brier/ECE denominators.

## Pair decisions with outcomes

Write the decision record before acting. It should contain only redacted or
content-addressed evidence and include:

- `decision_id`, timestamp, policy ID/version, request hash, model, and question;
- selected answer and the full relevant distribution;
- routing signal/basis and calibration probability/basis;
- threshold values, resulting route, and machine-readable reason codes;
- proposed action, authority decision, and risk class.

The proposed action is part of the decision input, not descriptive metadata
added afterward. Persist the verified judgment provenance and policy snapshot
with it so replay can recompute both the route and the authority decision.

Later append a minimal outcome record with the same `decision_id`:

- the observed ground-truth label in the question's declared outcome contract;
- the outcome timestamp.

The policy ledger intentionally stores the routing/calibration record and scalar
label, not the whole execution trace. The surrounding automation event should
content-address the expected postcondition, deterministic verifier, observed
evidence, realized cost, and evaluator provenance using the same `decision_id`;
see `schemas.md`.

Unknown, delayed, censored, or unobservable outcomes are not failures. Keep them
visible as decisions without an outcome event so a report counts them as missing
instead of silently treating them as correct or dropping them.

## Report calibration and drift

For labeled binary events with prediction `p_i` and outcome `o_i ∈ {0,1}`:

```text
Brier = mean((p_i - o_i)^2)
ECE   = sum_b (|B_b| / N) * |mean(p_i in B_b) - mean(o_i in B_b)|
```

Report at least labeled count, missing-outcome count, Brier, ECE, bucket counts,
mean probability, and empirical accuracy. Never combine cohorts with different
policy, model, question contract, or state projection merely to increase `N`.

Drift alerts need a frozen, identified baseline and a minimum sample count.
Compare recent Brier/ECE and per-bucket accuracy with that baseline; do not call
the first few samples a baseline, and do not alert on empty buckets. A drift
alert routes the policy back to shadow/review until a new evaluation is approved.

An externally supplied health report may only downgrade a route. Before an
automatic decision, recompute trusted health from the append-only event ledger
(the helper exposes this as `--health-events`) and verify that cohort, policy,
model, contract, and projection still match.

## Make consequence budget explicit

An economic check can inform—never authorize—the policy:

```text
expected_wrong_cost = (1 - calibrated_probability_of_correctness) * L_wrong
review_cost         = C_human
```

If `L_wrong` is unknown, the workflow does not know its autonomy budget. Prefer
review. Even when expected wrong cost is below review cost, deterministic
permission gates and mandatory checkpoints still win. Record estimates and
their provenance instead of presenting them as measured savings.

## Compact by selection, not summarization

For agent context or tool traces, decide what survives and preserve survivors
verbatim after redaction. A summary can change a path, error, constraint, or
authorization and later make the workflow reason from a fabricated history.

Use two distinct projections:

- **decision projection:** a bounded, redacted dossier sent to JEV to decide
  whether old tool calls/results remain useful;
- **canonical projection:** the exact redacted retained content returned to the
  executing agent, in original order, with hashes and stable IDs.

Rules:

- pair every tool call with its result; require an explicit completed marker on
  both records before they become eligible, and never retain an orphan result;
- pin the active user request, system/policy constraints, permission decisions,
  checkpoints, unresolved errors, current plan, uncommitted-change facts, and
  recent verification evidence;
- fan out independent retention questions over the same ordered state;
- permit only `keep verbatim`, deterministic truncation with byte/hash metadata,
  or removal of a complete eligible pair—never generated replacement prose;
- require a named policy version and calibrated threshold before drop/truncate;
- on missing key, timeout, malformed output, low signal, stale policy, or a state
  that cannot fit safely, retain the original or use a deterministic fallback;
- measure reduction, but do not accept a smaller context that loses a protected
  fact or breaks replay.

This is most useful for stale tool output. User and assistant intent, safety
constraints, and the evidence needed to verify unfinished work should not be
probabilistically rewritten or discarded.

`jev_trace_compact.py` verifies that the policy name and thresholds are explicit
and internally valid; the caller remains responsible for proving calibration
against the matching model, question contract, projection, and cohort.

## Promotion checklist

1. Define a versioned policy and explicit consequence budget.
2. Run in shadow mode and capture decision/outcome pairs.
3. Produce a cohort-specific calibration report with enough labeled samples.
4. Review failure slices, not only aggregate Brier/ECE.
5. Approve the exact policy version and limited autonomous action set; require
   a trusted judgment envelope and an exact proposed-action match.
6. Monitor missing outcomes, calibration drift, distribution shift, latency, and
   service failures; keep a deterministic kill switch/fallback.
7. Return to shadow mode after any model, question, projection, threshold, or
   action-semantics change.
