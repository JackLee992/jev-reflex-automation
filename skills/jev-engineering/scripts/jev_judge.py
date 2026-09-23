#!/usr/bin/env python3
"""Validate, redact, send, cache, and audit TypeSafe JEV judgments.

The default ``check`` command never performs a network call. ``run`` sends the
redacted request and records only that redacted state in its audit log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_CACHE_TTL_SECONDS = 86_400
CACHE_SCHEMA = "jev-engineering-cache-v2"
ALLOWED_TYPES = {"choice", "noul", "score"}
SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")

SENSITIVE_KEY_SUFFIXES = (
    "password", "passwd", "pwd", "secret", "token", "cookie", "session", "sessionid",
    "authorization", "apikey", "privatekey", "otp", "onetime", "2fa", "pin", "pincode",
    "credential", "credentials",
)
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted-private-key>"),
    (re.compile(r"(?i)(\b(?:authorization|proxy-authorization)\s*:)(?!\s*<redacted>)\s*[^\r\n]+"),
     r"\1 <redacted>"),
    (re.compile(r"(?i)(\b(?:cookie|set-cookie)\s*:)(?!\s*<redacted>)\s*[^\r\n]+"),
     r"\1 <redacted>"),
    (re.compile(r"(?i)\b(?:bearer|basic)\s+(?!<redacted>)[A-Za-z0-9._~+/=-]{8,}"),
     "credential <redacted>"),
    (re.compile(r"\bapikey_[A-Za-z0-9_=-]{32,}\b", re.I), "<redacted-typesafe-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
     "<redacted-jwt>"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-(?:ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"),
     "<redacted-secret>"),
    (re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    (re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|passwd|pwd|secret|cookie|session[_-]?id|authorization|token)"
        r"\s*[:=]\s*['\"]?(?!<redacted>)([^\s,'\";]{6,})"
    ), r"\1=<redacted>"),
    (re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "<redacted-email>"),
)
RESIDUAL_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:(?!\s*<redacted>)\s*[^\r\n]+"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+(?!<redacted>)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bapikey_[A-Za-z0-9_=-]{32,}\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-(?:ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(
        r"(?i)\b[A-Za-z][A-Za-z0-9_-]{0,40}(?:credential|credentials|secret|token|password|"
        r"private[_-]?key|api[_-]?key|apikey|session[_-]?id)\s*[:=]\s*(?!<redacted>)[^\s,;]{8,}"
    ),
)


class JevError(RuntimeError):
    """A safe, user-displayable JEV request error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(compact == suffix or compact.endswith(suffix) for suffix in SENSITIVE_KEY_SUFFIXES)


def _contains_residual_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_residual_secret(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_residual_secret(child) for child in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in RESIDUAL_SECRET_PATTERNS)
    return False


def redact(value: Any, *, key: str = "", max_string: int = 6000) -> tuple[Any, int]:
    """Recursively redact high-confidence secrets and clip oversized strings."""
    if key and _is_sensitive_key(key):
        if value == "<redacted-sensitive-field>":
            return value, 0
        return "<redacted-sensitive-field>", 1
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        count = 0
        for child_key, child in value.items():
            clean, n = redact(child, key=str(child_key), max_string=max_string)
            out[str(child_key)] = clean
            count += n
        return out, count
    if isinstance(value, list):
        out_list: list[Any] = []
        count = 0
        for child in value:
            clean, n = redact(child, max_string=max_string)
            out_list.append(clean)
            count += n
        return out_list, count
    if isinstance(value, str):
        clean = value
        count = 0
        for pattern, replacement in SECRET_PATTERNS:
            clean, n = pattern.subn(replacement, clean)
            count += n
        if len(clean) > max_string:
            clean = clean[:max_string] + f"…<clipped {len(clean) - max_string} chars>"
        return clean, count
    return value, 0


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise JevError("request must be a JSON object")
    state = payload.get("state")
    questions = payload.get("questions")
    if not isinstance(state, dict):
        raise JevError("request.state must be a JSON object")
    if not isinstance(questions, dict) or not questions:
        raise JevError("request.questions must be a non-empty object")
    if "model" in payload and (
        not isinstance(payload["model"], str) or not payload["model"].strip()
    ):
        raise JevError("request.model must be a non-empty string when present")
    if len(questions) > 128:
        raise JevError("request.questions exceeds the local safety limit of 128")

    for name, question in questions.items():
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name):
            raise JevError("question keys must be stable identifiers, not free text or sensitive data")
        if not isinstance(question, dict):
            raise JevError(f"question {name!r} must be an object")
        kind = question.get("type")
        if kind not in ALLOWED_TYPES:
            raise JevError(f"question {name!r} has unsupported type {kind!r}")
        if not isinstance(question.get("instructions"), str) or not question["instructions"].strip():
            raise JevError(f"question {name!r} needs non-empty instructions")
        criteria = question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
                raise JevError(f"choice {name!r} needs 2..255 criteria options")
            if any(not isinstance(k, str) or not SAFE_ID.fullmatch(k) for k in criteria):
                raise JevError(f"choice {name!r} option keys must be stable identifiers")
        if kind == "score":
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise JevError(f"score {name!r} criteria must be an ordered list with at least two levels")
    return payload


def prepare_request(payload: dict[str, Any], model: str, max_string: int = 6000) -> tuple[dict[str, Any], int, str]:
    validate_request(payload)
    if not isinstance(model, str) or not model.strip():
        raise JevError("model must be a non-empty string")
    if max_string < 200:
        raise JevError("max_string must be at least 200")
    body = {
        "model": model,
        "state": payload["state"],
        "questions": payload["questions"],
    }
    clean, redactions = redact(body, max_string=max_string)
    clean_again, additional_redactions = redact(clean, max_string=max_string)
    if additional_redactions or clean_again != clean or _contains_residual_secret(clean):
        raise JevError("request still appears to contain credentials after redaction; narrow or sanitize state")
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    request_hash = hashlib.sha256(encoded).hexdigest()
    return clean, redactions, request_hash


def load_api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".config" / "typesafe" / "api_key"
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise JevError("TYPESAFE_API_KEY is unset and ~/.config/typesafe/api_key is absent")
    return key


def validate_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise JevError(f"invalid JEV endpoint: {exc}") from None
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not local_http:
        raise JevError("endpoint must use HTTPS (plain HTTP is allowed only for localhost)")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise JevError("endpoint must not contain credentials, query parameters, or fragments")
    normalized = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()).geturl()
    return normalized.rstrip("/")


def _cache_key(request_hash: str, endpoint: str) -> str:
    identity = f"{CACHE_SCHEMA}\0{endpoint}\0{request_hash}".encode()
    return hashlib.sha256(identity).hexdigest()


def _cache_file(cache_dir: Path, cache_key: str) -> Path:
    return cache_dir / cache_key[:2] / f"{cache_key}.json"


def _read_cache(
    cache_dir: Path | None,
    request_hash: str,
    endpoint: str,
    ttl_seconds: float,
) -> dict[str, Any] | None:
    if cache_dir is None or ttl_seconds <= 0:
        return None
    path = _cache_file(cache_dir, _cache_key(request_hash, endpoint))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        stored_at = value.get("stored_at") if isinstance(value, dict) else None
        age = time.time() - stored_at if isinstance(stored_at, (int, float)) else math.inf
        if (
            isinstance(value, dict)
            and value.get("cache_schema") == CACHE_SCHEMA
            and value.get("endpoint") == endpoint
            and value.get("request_hash") == request_hash
            and -300 <= age <= ttl_seconds
            and isinstance(value.get("response"), dict)
            and isinstance(value["response"].get("answers"), dict)
        ):
            return value["response"]
        return None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_cache(
    cache_dir: Path | None,
    request_hash: str,
    endpoint: str,
    response: dict[str, Any],
    ttl_seconds: float,
) -> None:
    if cache_dir is None or ttl_seconds <= 0:
        return
    path = _cache_file(cache_dir, _cache_key(request_hash, endpoint))
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "cache_schema": CACHE_SCHEMA,
        "endpoint": endpoint,
        "request_hash": request_hash,
        "stored_at": time.time(),
        "response": response,
    }
    file_descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), 8.0) + random.uniform(0.0, 0.2)


def call_jev(
    body: dict[str, Any],
    *,
    endpoint: str,
    timeout: float,
    retries: int,
    api_key: str,
) -> tuple[dict[str, Any], float]:
    endpoint = validate_endpoint(endpoint)
    data = json.dumps(body, ensure_ascii=False).encode()
    started = time.monotonic()
    last: Exception | None = None
    retries = max(retries, 1)
    for attempt in range(retries):
        req = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                parsed = json.loads(response.read())
            if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
                raise JevError("JEV response is missing an answers object")
            return parsed, (time.monotonic() - started) * 1000
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", "replace")[:500]
            message, _ = redact(message, max_string=500)
            if exc.code not in {429, 529} and not 500 <= exc.code < 600:
                raise JevError(f"JEV HTTP {exc.code}: {message}") from None
            last = JevError(f"JEV HTTP {exc.code}: {message}")
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last = exc
            retry_after = None
        if attempt < retries - 1:
            time.sleep(_retry_delay(attempt, retry_after))
    raise JevError(f"JEV request failed after {retries} attempts: {last}") from None


def _probability(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise JevError(f"{label} must be a finite probability in [0, 1]")
    return float(value)


def _distribution(value: Any, expected_keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise JevError(f"{label} must contain exactly the requested levels")
    total = sum(_probability(probability, f"{label}.{key}") for key, probability in value.items())
    if not math.isclose(total, 1.0, abs_tol=0.05):
        raise JevError(f"{label} probabilities must sum to approximately 1")


def validate_response(body: dict[str, Any], result: Any) -> dict[str, Any]:
    """Reject malformed or out-of-contract answers before policy code sees them."""
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise JevError("JEV response is missing an answers object")
    if not isinstance(result.get("model"), str) or not result["model"]:
        raise JevError("JEV response is missing the resolved model")
    if result["model"] != body.get("model"):
        raise JevError("JEV response model does not match the pinned requested model")
    usage = result.get("usage")
    if not isinstance(usage, dict) or any(
        not isinstance(usage.get(key), int) or isinstance(usage.get(key), bool) or usage[key] < 0
        for key in ("input_tokens", "output_tokens")
    ):
        raise JevError("JEV response has invalid token usage")
    answers = result["answers"]
    if set(answers) != set(body["questions"]):
        raise JevError("JEV response answer keys do not exactly match the request")
    for name, question in body["questions"].items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise JevError(f"JEV response is missing answer {name!r}")
        kind = question["type"]
        if answer.get("type") != kind:
            raise JevError(f"JEV answer {name!r} has the wrong type")
        if kind == "noul":
            _probability(answer.get("noul"), f"JEV noul answer {name!r}")
        elif kind == "choice":
            choice = answer.get("choice")
            if choice not in question["criteria"]:
                raise JevError(f"JEV choice answer {name!r} selected an unknown option")
            _probability(answer.get("confidence"), f"JEV choice confidence {name!r}")
            _distribution(
                answer.get("probabilities"),
                set(question["criteria"]),
                f"JEV choice probabilities {name!r}",
            )
        elif kind == "score":
            score = answer.get("score")
            max_score = len(question["criteria"]) - 1
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
                or not 0 <= score <= max_score
            ):
                raise JevError(f"JEV score answer {name!r} must be finite and within its rubric")
            _probability(answer.get("confidence"), f"JEV score confidence {name!r}")
            expected_levels = {str(index) for index in range(len(question["criteria"]))}
            if not isinstance(answer.get("legend"), dict) or set(answer["legend"]) != expected_levels:
                raise JevError(f"JEV score legend {name!r} does not match its rubric")
            _distribution(
                answer.get("probabilities"),
                expected_levels,
                f"JEV score probabilities {name!r}",
            )
    return result


def append_audit(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    clean_event, _ = redact(event)
    if _contains_residual_secret(clean_event):
        raise JevError("audit event still appears to contain credentials after redaction")
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(clean_event, ensure_ascii=False, sort_keys=True) + "\n")


def run_request(
    payload: dict[str, Any],
    *,
    model: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 15.0,
    retries: int = 4,
    cache_dir: Path | None = None,
    audit: Path | None = None,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    max_string: int = 6000,
    api_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(cache_ttl_seconds, (int, float)) or not math.isfinite(cache_ttl_seconds):
        raise JevError("cache_ttl_seconds must be a finite number")
    resolved_model = model or payload.get("model") or os.environ.get("JEV_MODEL", DEFAULT_MODEL)
    body, redactions, request_hash = prepare_request(payload, resolved_model, max_string)
    normalized_endpoint = validate_endpoint(endpoint)
    started = time.monotonic()
    event_base = {
        "schema_version": "1",
        "ts": _utc_now(),
        "kind": "jev_judgment",
        "request_hash": request_hash,
        "requested_model": resolved_model,
        "endpoint": normalized_endpoint,
        "cache_schema": CACHE_SCHEMA,
        "question_keys": sorted(body["questions"]),
        "state_bytes": len(json.dumps(body["state"], ensure_ascii=False).encode()),
        "request_redactions": redactions,
        "request": body,
    }
    try:
        cached = _read_cache(cache_dir, request_hash, normalized_endpoint, cache_ttl_seconds)
        if cached is not None:
            result = dict(cached)
            latency_ms = 0.0
            from_cache = True
        else:
            result, latency_ms = call_jev(
                body,
                endpoint=normalized_endpoint,
                timeout=timeout,
                retries=retries,
                api_key=api_key or load_api_key(),
            )
            from_cache = False

        result = validate_response(body, result)
        clean_result, response_redactions = redact(result, max_string=max_string)
        if _contains_residual_secret(clean_result):
            raise JevError("JEV response still appears to contain credentials after redaction")
        if not from_cache:
            # Never persist fields that the service may echo before redacting them.
            _write_cache(cache_dir, request_hash, normalized_endpoint, clean_result, cache_ttl_seconds)
    except (JevError, OSError) as exc:
        append_audit(audit, {
            **event_base,
            "status": "error",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })
        raise

    event = {
        **event_base,
        "status": "ok",
        "response_model": clean_result.get("model"),
        "redactions": redactions + response_redactions,
        "latency_ms": round(latency_ms, 2),
        "cached": from_cache,
        "answers": clean_result.get("answers", {}),
        "usage": clean_result.get("usage"),
    }
    append_audit(audit, event)
    return {
        "response": clean_result,
        "meta": {k: event[k] for k in event if k not in {"answers", "usage", "request"}},
    }


def _load_json(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise JevError(f"cannot read request {path!r}: {exc}") from None


def _common_request_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("request", help="request JSON path, or - for stdin")
    parser.add_argument(
        "--model",
        help="override request.model and JEV_MODEL; defaults to the pinned local model",
    )
    parser.add_argument("--max-string", type=int, default=6000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate, redact, and preview without network access")
    _common_request_args(check)

    run = sub.add_parser("run", help="send a validated and redacted request")
    _common_request_args(run)
    run.add_argument("--endpoint", default=os.environ.get("JEV_ENDPOINT", DEFAULT_ENDPOINT))
    run.add_argument("--timeout", type=float, default=15.0)
    run.add_argument("--retries", type=int, default=4)
    run.add_argument("--cache-dir", type=Path)
    run.add_argument("--cache-ttl-seconds", type=float, default=DEFAULT_CACHE_TTL_SECONDS)
    run.add_argument("--audit", type=Path)

    audit = sub.add_parser("audit", help="read recent append-only audit events")
    audit.add_argument("path", type=Path)
    audit.add_argument("--tail", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            lines = args.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines[-max(args.tail, 0):] if line.strip()]
            print(json.dumps(events, ensure_ascii=False, indent=2))
            return 0

        payload = _load_json(args.request)
        model = args.model or payload.get("model") or os.environ.get("JEV_MODEL", DEFAULT_MODEL)
        if args.command == "check":
            body, redactions, request_hash = prepare_request(payload, model, args.max_string)
            print(json.dumps({
                "ok": True,
                "network": False,
                "request_hash": request_hash,
                "redactions": redactions,
                "request": body,
            }, ensure_ascii=False, indent=2))
            return 0

        result = run_request(
            payload,
            model=model,
            endpoint=args.endpoint,
            timeout=args.timeout,
            retries=max(args.retries, 1),
            cache_dir=args.cache_dir,
            audit=args.audit,
            cache_ttl_seconds=args.cache_ttl_seconds,
            max_string=args.max_string,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (JevError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
