from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

os.environ.setdefault("GWPLAYMATE_DISABLE_MEMORY_WRITES", "1")

import backend.hermes.daemon as hermes_daemon
from backend.hermes.gw1_knowledge import resolve_gw1_context
from backend.hermes_eval.agents import review_failures
from backend.hermes_eval.cases import EvalCase, iter_eval_cases


@dataclass
class EvalFailure:
    case_id: str
    expected_intent: str
    expected_topic: str
    message: str
    reply: str
    reasons: tuple[str, ...]
    severity: str


@dataclass
class EvalSummary:
    total: int
    passed: int
    failed: int
    critical_failed: int
    fallback_like: int
    elapsed_seconds: float
    pass_rate: float


FALLBACK_LIKE_PATTERN = re.compile(
    r"\b(?:one more detail|tell me what you'?re looking at|could be|maybe)\b",
    re.IGNORECASE,
)


def score_case(case: EvalCase) -> tuple[bool, EvalFailure | None, bool]:
    context = resolve_gw1_context(case.event, case.recent_context)
    decision = hermes_daemon.fallback_rule_decision(case.event)
    reply = decision.response or ""
    reasons: list[str] = []

    if case.expected_intent != "combat":
        if context.intent != case.expected_intent:
            reasons.append(f"intent={context.intent!r}")
        if context.canonical_topic != case.expected_topic:
            reasons.append(f"topic={context.canonical_topic!r}")
    else:
        if not decision.should_speak:
            reasons.append("combat_silent")

    lower_reply = reply.lower()
    for anchor in case.required_anchors:
        if anchor.lower() not in lower_reply:
            reasons.append(f"missing_anchor={anchor!r}")
    for forbidden in case.forbidden_patterns:
        if forbidden.lower() in lower_reply:
            reasons.append(f"forbidden={forbidden!r}")
    if decision.should_speak and len(reply) > 119:
        reasons.append("reply_too_long")

    fallback_like = bool(FALLBACK_LIKE_PATTERN.search(reply))
    if fallback_like and case.expected_intent not in {"unknown", "combat"}:
        reasons.append("generic_fallback_like")

    if reasons:
        return (
            False,
            EvalFailure(
                case_id=case.id,
                expected_intent=case.expected_intent,
                expected_topic=case.expected_topic,
                message=case.event.message,
                reply=reply,
                reasons=tuple(reasons),
                severity=case.severity,
            ),
            fallback_like,
        )
    return True, None, fallback_like


def run_eval(total: int, failure_limit: int) -> tuple[EvalSummary, list[EvalFailure]]:
    original_recent_reply_lines = hermes_daemon.recent_reply_lines
    original_recent_conversation_context = hermes_daemon.recent_conversation_context
    hermes_daemon.recent_reply_lines = lambda *args, **kwargs: []
    hermes_daemon.recent_conversation_context = lambda *args, **kwargs: ""
    try:
        started = perf_counter()
        passed = 0
        fallback_like = 0
        failures: list[EvalFailure] = []
        critical_failed = 0

        for case in iter_eval_cases(total):
            ok, failure, is_fallback_like = score_case(case)
            fallback_like += int(is_fallback_like)
            if ok:
                passed += 1
                continue
            if failure and failure.severity == "critical":
                critical_failed += 1
            if failure and len(failures) < failure_limit:
                failures.append(failure)

        elapsed = perf_counter() - started
        failed = total - passed
        return (
            EvalSummary(
                total=total,
                passed=passed,
                failed=failed,
                critical_failed=critical_failed,
                fallback_like=fallback_like,
                elapsed_seconds=elapsed,
                pass_rate=passed / max(total, 1),
            ),
            failures,
        )
    finally:
        hermes_daemon.recent_reply_lines = original_recent_reply_lines
        hermes_daemon.recent_conversation_context = original_recent_conversation_context


def write_reports(summary: EvalSummary, failures: list[EvalFailure], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "failures.jsonl").write_text(
        "\n".join(json.dumps(asdict(failure), ensure_ascii=False) for failure in failures) + ("\n" if failures else ""),
        encoding="utf-8",
    )
    lines = [
        "# Hermes Synthetic Dialogue Eval",
        "",
        f"- total: {summary.total}",
        f"- passed: {summary.passed}",
        f"- failed: {summary.failed}",
        f"- critical_failed: {summary.critical_failed}",
        f"- fallback_like: {summary.fallback_like}",
        f"- pass_rate: {summary.pass_rate:.4%}",
        f"- elapsed_seconds: {summary.elapsed_seconds:.2f}",
        "",
        "## Sample Failures",
    ]
    for failure in failures[:25]:
        lines.extend(
            [
                "",
                f"### {failure.case_id}",
                f"- message: {failure.message}",
                f"- reply: {failure.reply}",
                f"- reasons: {', '.join(failure.reasons)}",
            ]
        )
    grouped = review_failures(failures)
    if grouped:
        lines.extend(["", "## Reviewer Queues"])
        for reviewer, findings in grouped.items():
            lines.extend(["", f"### {reviewer}", f"- findings: {len(findings)}"])
            for finding in findings[:10]:
                lines.append(f"- {finding.case_id}: {finding.recommendation}")
    (output_dir / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline Azele synthetic dialogue evals.")
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--cases", type=int, default=None)
    parser.add_argument("--failure-limit", type=int, default=5000)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/hermes_eval"))
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    total = args.cases if args.cases is not None else (250_000 if args.profile == "full" else 500)
    summary, failures = run_eval(total=total, failure_limit=args.failure_limit)
    if not args.no_report:
        write_reports(summary, failures, args.output_dir)

    print(json.dumps(asdict(summary), indent=2))
    if summary.critical_failed:
        return 2
    if summary.pass_rate < 0.995:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
