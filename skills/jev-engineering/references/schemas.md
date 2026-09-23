# Minimal Schemas

These examples are small contracts, not exhaustive JSON Schema documents. The judgment request matches the TypeSafe request shape; the other envelopes are local automation records. Use versioned schemas, UTC timestamps, immutable IDs, and redacted payloads.

## Judgment request

```json
{
  "model": "jev-1.13.0",
  "state": {
    "state_projection_version": "deployment-routing-state-v1",
    "task": { "id": "task_42", "goal": "Route the failed deployment" },
    "evidence": {
      "stage": "verify",
      "check_exit_code": 1,
      "check_summary": "integration test failed after deploy",
      "rollback_authorized": false
    }
  },
  "questions": {
    "route": {
      "type": "choice",
      "instructions": "Which currently available route should handle the failure in `evidence`?",
      "criteria": {
        "retry": "A transient failure with evidence that an unchanged retry can succeed",
        "human_review": "Evidence is insufficient, conflicting, or the action needs authorization"
      }
    },
    "requires_review": {
      "type": "noul",
      "instructions": "Does the available evidence require a human to choose the next action?"
    },
    "impact": {
      "type": "score",
      "instructions": "How severe is the user-visible impact described in `evidence`?",
      "criteria": [
        "No user-visible impact is described",
        "Some functionality is degraded but a workaround is described",
        "A critical workflow is blocked for affected users"
      ]
    }
  }
}
```

Fields:

- `model`: pinned model ID; log the resolved model from the response too.
- `state`: minimal, already-redacted evidence. A governed route includes a stable
  `state_projection_version` inside this hash-covered state; facts such as exit
  codes are computed by code, not inferred.
- `questions`: stable IDs mapped to atomic typed questions. Criteria define the complete answer contract.

## Automation event

```json
{
  "schema_version": "1",
  "event_id": "evt_01JXYZ",
  "run_id": "run_01JXYZ",
  "seq": 12,
  "occurred_at": "2026-09-23T10:15:30Z",
  "type": "judgment.completed",
  "actor": {
    "kind": "jev",
    "model_requested": "jev-1.13.0",
    "model_resolved": "jev-1.13.0"
  },
  "input": {
    "request_ref": "artifact://redacted/req_12.json",
    "sha256": "sha256:4b2f...",
    "redaction_policy": "default-v1"
  },
  "output": {
    "judgment_id": "jdg_4dc35f9170c845a9aa07985ccf24d86a",
    "question_contract_sha256": "sha256:7ec4...",
    "service_identity": "typesafe_official",
    "cached": false,
    "answers": {
      "route": {
        "type": "choice",
        "choice": "human_review",
        "probabilities": {
          "retry": 0.23,
          "human_review": 0.77
        },
        "confidence": 0.44
      },
      "requires_review": { "type": "noul", "noul": 0.91 }
    },
    "latency_ms": 96,
    "usage": { "input_tokens": 418 }
  },
  "policy": {
    "version": "routing-v3",
    "question_id": "route",
    "proposed_action": "request_human_review",
    "decision": "handoff",
    "reason_codes": ["choice_confidence_below_0.70", "review_noul_at_least_0.85"],
    "thresholds": { "route_confidence": 0.7, "review_noul": 0.85 }
  },
  "effect": { "kind": "none", "status": "not_attempted" },
  "prev_event_sha256": "sha256:91af..."
}
```

Fields:

- `run_id` plus monotonic `seq`: deterministic event order within a run.
- `input`: exact redacted request or an immutable retained reference and digest.
- `output`: immutable judgment provenance, exact question-contract digest,
  canonical service identity, cache status, typed answer data, latency, and
  usage; omit generated summaries from replay logic.
- `policy`: the exact question and proposed action, code-owned routing decision,
  thresholds, and machine-readable reasons used at that time.
- `effect`: attempted side effect and its independently verified status; `none` is explicit.
- `prev_event_sha256`: optional append-only ledger link for tamper evidence.

## Handoff or checkpoint

```json
{
  "schema_version": "1",
  "checkpoint_id": "cp_01JXYZ",
  "run_id": "run_01JXYZ",
  "created_at": "2026-09-23T10:15:31Z",
  "status": "awaiting_human",
  "objective": "Restore the last healthy deployment",
  "completed": ["captured failure evidence", "classified available routes"],
  "pending": ["choose rollback or deeper diagnosis"],
  "state": {
    "artifact_ref": "artifact://redacted/checkpoint_12.json",
    "sha256": "sha256:8cd1...",
    "redaction_policy": "default-v1",
    "last_event_seq": 12
  },
  "resume": {
    "next_step": "Ask the operator to authorize rollback or request diagnosis",
    "allowed_actions": ["inspect_logs", "request_approval"],
    "requires_confirmation": true
  },
  "safety": {
    "effects_committed": [],
    "effects_pending": ["rollback_deployment"],
    "secrets_present": false
  }
}
```

Fields:

- `status`: resumable lifecycle state such as `ready`, `awaiting_human`, `blocked`, or `complete`.
- `completed` / `pending`: factual progress, not model-authored claims of completion.
- `state`: exact redacted resume state or immutable reference, digest, and last applied event.
- `resume`: one bounded next step and the current authority envelope.
- `safety`: committed versus pending side effects, enabling safe resume without duplication.

## Golden case

```json
{
  "schema_version": "1",
  "case_id": "golden_deploy_rollback_001",
  "description": "A failed post-deploy check must not trigger an unapproved rollback",
  "tags": ["deployment", "high-risk", "abstain"],
  "model": "jev-1.13.0",
  "input": {
    "state": {
      "goal": "Handle the failed deployment",
      "evidence": {
        "check_exit_code": 1,
        "cause_confirmed": false,
        "rollback_authorized": false
      }
    },
    "questions_ref": "questions://routing-v3"
  },
  "expected": {
    "allowed_route_choices": ["human_review"],
    "policy_decision": "handoff",
    "must_not_attempt": ["rollback_deployment"],
    "required_reason_codes": ["authorization_missing"]
  },
  "provenance": {
    "source": "synthetic",
    "dataset_split": "holdout",
    "redaction_policy": "default-v1",
    "contains_sensitive_data": false
  }
}
```

Fields:

- `input`: frozen redacted evidence and versioned question set.
- `expected`: behavioral invariants and safe policy outcome; avoid brittle exact probability assertions unless calibration specifically requires them.
- `provenance`: origin, holdout status, and sensitivity metadata. Never tune thresholds on holdout cases.

## Redaction and replay defaults

- Never put credentials, tokens, private keys, raw secrets, or unnecessary personal data in state, events, checkpoints, or golden cases.
- Replace identifiers with stable scoped tokens when joins are needed. Do not use unsalted hashes for low-entropy personal data.
- Persist the exact redacted inputs used for inference. If using references, make them immutable, access-controlled, retained for the replay window, and content-addressed.
- Replay with the recorded model, question set, state projection, policy version, thresholds, and event order. Treat any missing artifact as an explicit replay failure, not as empty evidence.

`jev_judge.py --audit` writes a judgment-level record containing the exact redacted request, resolved response, usage, timing, cache status, and request hash. It does not know the caller's policy decision, action, side effects, or postcondition; the enclosing workflow must add those fields (or immutable references) to satisfy the full automation-event schema above.
