from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from typing import Iterator

from backend.shared.models import TelemetryEvent


@dataclass(frozen=True)
class EvalCase:
    id: str
    event: TelemetryEvent
    expected_intent: str
    expected_topic: str
    required_anchors: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    recent_context: tuple[str, ...] = ()
    severity: str = "normal"


def _player_event(message: str, **overrides: object) -> TelemetryEvent:
    data = {
        "event_type": "player_chat",
        "sender": "Player",
        "channel": "party",
        "message": message,
        "persona": "Azele",
        "map_id": 148,
        "map_name": "Ascalon City",
        "session_id": "eval",
    }
    data.update(overrides)
    return TelemetryEvent(**data)


def _alert_event(message: str, **overrides: object) -> TelemetryEvent:
    data = {
        "event_type": "environment_alert",
        "sender": "System",
        "channel": "system",
        "message": message,
        "persona": "Azele",
        "alert_type": "under_attack",
        "severity": "HIGH",
        "map_id": 148,
        "map_name": "Ascalon City",
        "player_hp": 0.42,
        "player_hp_previous": 0.56,
        "player_hp_drop": 0.14,
        "hp_threshold_crossed": "50%",
        "damage_severity": "heavy",
        "session_id": "eval",
    }
    data.update(overrides)
    return TelemetryEvent(**data)


BASE_DIALOGUE_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        id="scourge-tunnel-run",
        event=_player_event(
            "wanna do another tunnel run?",
            map_id=779,
            map_name="Piken Square",
            active_quest_id=1456,
            active_quest_name="The Scourge Beneath",
        ),
        expected_intent="quest",
        expected_topic="The Scourge Beneath",
        required_anchors=("Scourge",),
        forbidden_patterns=("generic tunnel", "one more detail"),
        severity="critical",
    ),
    EvalCase(
        id="scourge-short-ask",
        event=_player_event(
            "wanna do scourge?",
            map_id=148,
            map_name="Ascalon City",
            active_quest_id=1456,
            active_quest_name="",
        ),
        expected_intent="quest",
        expected_topic="The Scourge Beneath",
        required_anchors=("Scourge",),
        forbidden_patterns=("one more detail", "tell me what", "could be", "maybe"),
        severity="critical",
    ),
    EvalCase(
        id="ldoa-slang",
        event=_player_event("what's the LDoA plan from here?", map_name="Ashford Abbey"),
        expected_intent="title",
        expected_topic="Legendary Defender of Ascalon",
        required_anchors=("level", "pre"),
        severity="critical",
    ),
    EvalCase(
        id="charr-hunt",
        event=_player_event("let's level you then hunt Charr past the wall", map_name="Ascalon City"),
        expected_intent="enemy",
        expected_topic="Charr",
        required_anchors=("Charr", "Ascalon"),
        forbidden_patterns=("save the Charr",),
        severity="critical",
    ),
    EvalCase(
        id="black-dye",
        event=_player_event("black dye dropped from that Charr Axe Fiend", agent_name="Charr Axe Fiend"),
        expected_intent="loot",
        expected_topic="Black Dye",
        required_anchors=("Black Dye",),
        severity="critical",
    ),
    EvalCase(
        id="purple-drop",
        event=_player_event("ooo a purple thing dropped", map_name="Fort Ranik"),
        expected_intent="loot",
        expected_topic="Purple rarity loot",
        required_anchors=("Purple",),
        forbidden_patterns=("what purple thing",),
    ),
    EvalCase(
        id="krytan-leggings",
        event=_player_event("the Krytan leggings are an upgrade but the skirt is longer"),
        expected_intent="gear",
        expected_topic="Krytan Leggings",
        required_anchors=("upgrade",),
        forbidden_patterns=("your leggings", "your skirt"),
        severity="critical",
    ),
    EvalCase(
        id="devona-pet",
        event=_player_event("what pet should we get Devona? warthog or stalker?"),
        expected_intent="npc",
        expected_topic="Devona pet choice",
        required_anchors=("Devona",),
    ),
    EvalCase(
        id="red-iris-bag",
        event=_player_event("one more red iris and we get more bag space"),
        expected_intent="item",
        expected_topic="Red Iris Flower",
        required_anchors=("iris", "bag"),
    ),
)


DAMAGE_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        id="damage-heavy-half",
        event=_alert_event("Azele is taking hits. Health is at 42 percent.", player_hp=0.42, player_hp_previous=0.56, player_hp_drop=0.14, hp_threshold_crossed="50%", damage_severity="heavy"),
        expected_intent="combat",
        expected_topic="under_attack",
        required_anchors=("42%",),
        severity="critical",
    ),
    EvalCase(
        id="damage-near-death",
        event=_alert_event("Azele is almost down.", player_hp=0.18, player_hp_previous=0.31, player_hp_drop=0.13, hp_threshold_crossed="20%", damage_severity="near_death"),
        expected_intent="combat",
        expected_topic="under_attack",
        required_anchors=("18%",),
        severity="critical",
    ),
)


TYPO_VARIANTS: tuple[tuple[str, str], ...] = (
    ("scourge", "scorge"),
    ("tunnel", "tunel"),
    ("another", "anudder"),
    ("krytan", "kyrtan"),
    ("leggings", "leggins"),
    ("charr", "char"),
    ("purple", "pruple"),
    ("devona", "devonna"),
)


def mutate_message(message: str, index: int) -> str:
    mutated = message
    for offset, (source, target) in enumerate(TYPO_VARIANTS):
        if (index + offset) % 5 == 0:
            mutated = mutated.replace(source, target).replace(source.title(), target.title())
    if index % 7 == 0:
        mutated = mutated.replace("?", "")
    if index % 11 == 0:
        mutated = mutated.lower()
    if index % 13 == 0:
        mutated = f"uh {mutated}"
    return mutated


def iter_eval_cases(total: int = 250_000) -> Iterator[EvalCase]:
    bases = BASE_DIALOGUE_CASES + DAMAGE_CASES
    for index in range(total):
        base = bases[index % len(bases)]
        if base.event.event_type == "player_chat":
            event = base.event.model_copy(update={"message": mutate_message(base.event.message, index)})
        else:
            event = base.event
        yield EvalCase(
            id=f"{base.id}-{index:06d}",
            event=event,
            expected_intent=base.expected_intent,
            expected_topic=base.expected_topic,
            required_anchors=base.required_anchors,
            forbidden_patterns=base.forbidden_patterns,
            recent_context=base.recent_context,
            severity=base.severity,
        )


def sample_eval_cases(total: int) -> tuple[EvalCase, ...]:
    return tuple(islice(iter_eval_cases(total), total))
