from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReviewFinding:
    reviewer: str
    case_id: str
    recommendation: str


def review_failure(failure: Any) -> tuple[ReviewFinding, ...]:
    findings: list[ReviewFinding] = []
    reasons = " ".join(failure.reasons)
    if "intent=" in reasons or "topic=" in reasons:
        findings.append(
            ReviewFinding(
                reviewer="Resolver Critic",
                case_id=failure.case_id,
                recommendation="Add or reprioritize GW1 aliases/context ranking for this phrase.",
            )
        )
    if failure.expected_intent in {"quest", "title", "enemy", "loot"} and "missing_anchor" in reasons:
        findings.append(
            ReviewFinding(
                reviewer="Lore Auditor",
                case_id=failure.case_id,
                recommendation="Add required lore anchors or strengthen the deterministic reply.",
            )
        )
    if "generic_fallback_like" in reasons or "forbidden=" in reasons:
        findings.append(
            ReviewFinding(
                reviewer="Conversation Judge",
                case_id=failure.case_id,
                recommendation="Teach the reply path to answer the player's immediate intent before personality.",
            )
        )
    if failure.expected_intent == "combat":
        findings.append(
            ReviewFinding(
                reviewer="Combat Judge",
                case_id=failure.case_id,
                recommendation="Adjust damage thresholds, cooldowns, or short combat bark wording.",
            )
        )
    if findings:
        findings.append(
            ReviewFinding(
                reviewer="Regression Miner",
                case_id=failure.case_id,
                recommendation="Convert this failure into a permanent unit test or eval fixture.",
            )
        )
    return tuple(findings)


def review_failures(failures: list[Any]) -> dict[str, list[ReviewFinding]]:
    grouped: dict[str, list[ReviewFinding]] = {}
    for failure in failures:
        for finding in review_failure(failure):
            grouped.setdefault(finding.reviewer, []).append(finding)
    return grouped
