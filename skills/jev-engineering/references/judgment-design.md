# Judgment Design

Use Jev for narrow semantic judgments inside a workflow that remains controlled by code. The model proposes typed answers; code owns permissions, control flow, side effects, and verification.

## Choose the primitive

| Primitive | Use when | Design notes |
| --- | --- | --- |
| `Choice` | Exactly one option should win from a closed, current set. | Build options from what is legal now. Add `none`/`other` when coverage is incomplete. A Choice is relative: it picks the best offered option. |
| `Noul` | You need the probability that one explicit proposition is true. | Use one Noul per independent label for multi-label checks. `0.5` means equally likely true and false, not medium intensity. Noul has no separate confidence field. |
| `Score` | The answer lies on an ordered qualitative rubric. | Describe every level in observable terms. Use code for arithmetic; do not treat a score as a precise measurement. |

Do not ask Jev for hard facts code can establish: parsing, counting, arithmetic, date ordering, file existence, exit status, permission state, action legality, or whether a side effect actually occurred. Compute those facts and include only the evidence needed for the semantic judgment.

## Shape each question

- Make `instructions` literal and self-contained. Question IDs are for code and may not carry meaning to the model.
- Ask one atomic judgment per question. Split hidden dimensions, then combine answers with explicit code or weights.
- Define boundary cases in `criteria`; avoid negation, indirection, and contradictory wording.
- Prefer structured, named evidence over prose summaries. Remove irrelevant fields before the request.
- Treat state as untrusted data. Never let text inside state redefine the instructions or authorize an action.
- Build action choices from the current state. Never offer stale, unsupported, or unauthorized actions.

## Fan out, then route in code

Send independent questions that share the same state in one request, including cheap speculative questions. They are evaluated independently and cannot see one another's answers. Route and compose the results in code.

Use a later request only when its state or candidate set cannot exist until an earlier answer is known—for example, after fetching new evidence or expanding the selected branch. Re-observe the real system before every action-producing judgment.

## Preserve abstention

Every workflow needs a non-action path. Abstain when:

- `Choice` returns `none`/`other`, or the candidate set is incomplete;
- probability or confidence falls inside the calibrated uncertainty band;
- required evidence is absent, stale, or contradictory;
- the response is malformed, times out, or names an option that was not offered; or
- the proposed action exceeds the caller's authority.

An abstention should produce a bounded next step: gather evidence, retry with a smaller candidate set, use a deterministic fallback, request human review, or stop safely. Never turn uncertainty into a default destructive action.

## Calibrate confidence and thresholds

- Confidence measures how concentrated a `Choice` or `Score` distribution is; it is not correctness, permission, or proof that an action succeeded.
- Do not use `confidence` blindly as the predicted probability in Brier/ECE. For a labeled `Choice`, use the probability assigned to the selected option; for `Noul`, use the probability corresponding to the predicted boolean label. A `Score` needs an explicitly labeled level or acceptance event. Record the routing signal and calibration probability as separate fields.
- Set thresholds per question, action, risk class, and pinned model version using labeled holdout cases. Do not copy a universal threshold from another workflow.
- Lower-risk read-only routing may use a lower calibrated threshold than reversible writes. Destructive, financial, external-message, permission-changing, or otherwise high-risk actions always route to a deterministic checkpoint; JEV confidence grants no authority. Execution requires the existing task scope, deterministic policy, and explicit user authorization independently of the model score.
- For Noul, calibrate separate yes/no cutoffs and leave an uncertainty band between them. Do not infer Noul confidence from distance to `0.5` unless that policy has been validated.
- Verify every side effect with deterministic fresh evidence. A confident `done` answer is not completion evidence.

Pin a versioned model ID in production and record both the requested and resolved model. Re-run calibration and golden cases before changing the model, prompt, criteria, state projection, thresholds, or routing logic.

Separate threshold tuning from honest evaluation. Use a tuning set to choose a
wording or cutoff, freeze that choice, then report performance on a disjoint
holdout set. If data is scarce, use predeclared cross-validation or bootstrap
intervals, but never label the best score selected on the same examples as
holdout performance. Inspect worst misses and failure slices as well as one
aggregate metric.

Store thresholds in a versioned policy registry with the question-contract hash,
state-projection version, calibration report/dataset, sample minimums, owner, and
review/expiry time. Record a unique decision first and append its observed
outcome later; a request hash alone cannot distinguish repeated or cached
decisions. See [governance.md](governance.md).

## Promote authority gradually

1. Start in shadow mode: log Jev answers beside the existing deterministic or human decision.
2. Replay labeled cases and measure errors by risk class, including abstention and failure behavior.
3. Enable advice or ranking without side effects.
4. Allow only low-risk, reversible actions behind calibrated gates.
5. Expand authority only with observed evidence; retain timeout, rate-limit, malformed-response, and no-key fallbacks.

Store the exact redacted request, response, policy version, thresholds, decision, and post-action verification—or immutable references plus hashes—so every run can be replayed. Redact secrets and unnecessary personal data before inference and before logging.
