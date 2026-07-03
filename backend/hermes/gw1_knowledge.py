from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    canonical_name: str
    aliases: tuple[str, ...]
    category: str
    era_scope: str
    response_anchors: tuple[str, ...]
    tags: tuple[str, ...] = ()
    required_context: tuple[str, ...] = ()
    confidence: float = 0.86


@dataclass(frozen=True)
class ResolvedGameContext:
    intent: str
    canonical_topic: str
    aliases: tuple[str, ...] = ()
    response_anchors: tuple[str, ...] = ()
    era_scope: str = "all"
    confidence: float = 0.0
    entry_id: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return bool(self.entry_id)


GW1_KNOWLEDGE: tuple[KnowledgeEntry, ...] = (
    KnowledgeEntry(
        id="quest.scourge_beneath",
        canonical_name="The Scourge Beneath",
        aliases=(
            "a scourge beneath",
            "scourge beneath",
            "scorge beneath",
            "scourge below",
            "scorge below",
            "scourge",
            "scorge",
            "maz scourgeheart",
            "forsaken tunnels",
            "tunnel run",
            "tunel run",
            "another tunnel run",
            "anudder tunel run",
            "run the tunnels",
            "run the tunels",
            "tunnels again",
        ),
        category="quest",
        era_scope="pre_searing",
        response_anchors=("The Scourge Beneath", "Forsaken Tunnels", "Maz Scourgeheart", "Devona"),
        tags=("ldoa", "northlands", "piken_square", "elementals"),
        required_context=("pre_searing",),
        confidence=0.94,
    ),
    KnowledgeEntry(
        id="title.ldoa",
        canonical_name="Legendary Defender of Ascalon",
        aliases=(
            "ldoa",
            "legendary defender",
            "legendary defender of ascalon",
            "defender of ascalon",
            "level 20 in pre",
            "pre searing grind",
            "death leveling",
            "langmar daily",
            "vanguard daily",
        ),
        category="title",
        era_scope="pre_searing",
        response_anchors=("level 20", "pre-Searing Ascalon", "Langmar dailies", "Vanguard"),
        tags=("progression", "pre_searing"),
        confidence=0.92,
    ),
    KnowledgeEntry(
        id="enemy.charr",
        canonical_name="Charr",
        aliases=("charr", "char", "charr hunting", "hunt charr", "fight charr", "kill charr", "northlands charr"),
        category="enemy",
        era_scope="pre_searing",
        response_anchors=("Charr", "Ascalon", "the Wall", "Northlands"),
        tags=("ascalon", "threat"),
        confidence=0.9,
    ),
    KnowledgeEntry(
        id="loot.black_dye",
        canonical_name="Black Dye",
        aliases=("black dye", "rare dye", "black vial", "black dye drop"),
        category="loot",
        era_scope="all",
        response_anchors=("Black Dye", "pre-Searing", "rare"),
        tags=("rare_drop", "dye"),
        confidence=0.91,
    ),
    KnowledgeEntry(
        id="loot.purple",
        canonical_name="Purple rarity loot",
        aliases=(
            "purple",
            "purp",
            "pruple",
            "purple drop",
            "purp drop",
            "purple thing",
            "purple item",
            "purple rarity",
            "purple hammer",
            "purp hammer",
        ),
        category="loot",
        era_scope="all",
        response_anchors=("Purple", "worth a look", "what it rolled"),
        tags=("rare_drop", "rarity"),
        confidence=0.84,
    ),
    KnowledgeEntry(
        id="item.red_iris",
        canonical_name="Red Iris Flower",
        aliases=("red iris", "red irises", "iris", "irises", "flower for bag", "bag flower"),
        category="item",
        era_scope="pre_searing",
        response_anchors=("red iris", "bag space", "pre-Searing"),
        tags=("bag_space", "collector"),
        confidence=0.86,
    ),
    KnowledgeEntry(
        id="gear.krytan_leggings",
        canonical_name="Krytan Leggings",
        aliases=("krytan leggings", "kyrtan leggins", "leggings", "leggins", "longer skirt", "mini skirt", "skirt upgrade"),
        category="gear",
        era_scope="pre_searing",
        response_anchors=("Krytan leggings", "upgrade", "style"),
        tags=("armor", "aesthetic"),
        confidence=0.88,
    ),
    KnowledgeEntry(
        id="npc.devona_pet",
        canonical_name="Devona pet choice",
        aliases=("devona", "devonna", "devona pet", "devonna pet", "pet for devona", "pet for devonna", "warthog for devona", "stalker for devona"),
        category="npc",
        era_scope="pre_searing",
        response_anchors=("Devona", "pet", "stalker", "warthog"),
        tags=("ranger_pet", "companion"),
        confidence=0.83,
    ),
    KnowledgeEntry(
        id="quest.vanguard_rescue_gaban",
        canonical_name="Vanguard Rescue: Save the Ascalonian Noble",
        aliases=(
            "duke gaban",
            "gaban",
            "save duke gaban",
            "save the ascalonian noble",
            "ascalonian noble",
            "vanguard rescue",
            "where is duke gaban",
        ),
        category="quest",
        era_scope="pre_searing",
        response_anchors=("Duke Gaban", "Catacombs", "search", "escort"),
        tags=("catacombs", "vanguard", "rescue", "ascalon"),
        confidence=0.9,
    ),
    KnowledgeEntry(
        id="map.ascalon_city",
        canonical_name="Ascalon City",
        aliases=("ascalon city", "ascalon", "city"),
        category="map",
        era_scope="pre_searing",
        response_anchors=("Ascalon City", "home", "people"),
        tags=("pre_searing", "ascalon"),
        confidence=0.8,
    ),
    KnowledgeEntry(
        id="map.fort_ranik",
        canonical_name="Fort Ranik",
        aliases=("fort ranik", "ranik"),
        category="map",
        era_scope="pre_searing",
        response_anchors=("Fort Ranik", "south", "Ascalon"),
        tags=("pre_searing", "ascalon"),
        confidence=0.78,
    ),
)


def _read_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _recent_text(recent_context: str | Iterable[str] | None) -> str:
    if not recent_context:
        return ""
    if isinstance(recent_context, str):
        return _read_text(recent_context).lower()
    return _read_text(" ".join(str(item) for item in recent_context)).lower()


def _event_context_text(event: Any) -> str:
    parts = [
        getattr(event, "map_name", ""),
        getattr(event, "active_quest_name", ""),
        getattr(event, "active_quest_objectives", ""),
        getattr(event, "agent_name", ""),
        str(getattr(event, "map_id", "") or ""),
        str(getattr(event, "active_quest_id", "") or ""),
    ]
    return _read_text(" ".join(parts)).lower()


def _pre_searing_score(event: Any, text: str) -> float:
    score = 0.0
    if "pre-searing" in text or "pre searing" in text or "ascalon" in text:
        score += 0.06
    if getattr(event, "map_id", 0) in {148, 164, 165, 166, 176, 177, 178, 179, 181, 182, 183, 184, 188, 191, 779}:
        score += 0.08
    if getattr(event, "active_quest_id", 0) == 1456:
        score += 0.12
    return score


def _alias_matches(alias: str, text: str) -> bool:
    escaped = re.escape(alias.lower()).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){escaped}(?!\w)", text))


def _find_entry(entry_id: str) -> KnowledgeEntry | None:
    for entry in GW1_KNOWLEDGE:
        if entry.id == entry_id:
            return entry
    return None


def _looks_like_gaban_followup(message_text: str, event_text: str, recent_text: str) -> bool:
    if not re.search(r"\b(?:duke\s+)?gaban\b|\bascalonian noble\b", recent_text):
        return False
    if not re.search(r"\b(?:he|him|his|somewhere|spots?|where|find|look|search|hide|hiding|might|could|think)\b", message_text):
        return False
    combined = f"{message_text} {event_text}"
    return bool(re.search(r"\bcatacombs?\b", combined) or re.search(r"\b(?:map_id|area_id)\s*[:=]?\s*(?:145|151)\b", combined))


def resolve_gw1_context(event: Any, recent_context: str | Iterable[str] | None = None) -> ResolvedGameContext:
    message_text = _read_text(getattr(event, "message", "")).lower()
    event_text = _event_context_text(event)
    recent_text = _recent_text(recent_context)
    combined_text = " ".join(part for part in (message_text, event_text, recent_text) if part)
    if not combined_text:
        return ResolvedGameContext(intent="unknown", canonical_topic="")

    gaban_entry = _find_entry("quest.vanguard_rescue_gaban")
    if gaban_entry and _looks_like_gaban_followup(message_text, event_text, recent_text):
        score = min(gaban_entry.confidence + 0.06 + _pre_searing_score(event, combined_text), 0.99)
        return ResolvedGameContext(
            intent=gaban_entry.category,
            canonical_topic=gaban_entry.canonical_name,
            aliases=gaban_entry.aliases,
            response_anchors=gaban_entry.response_anchors,
            era_scope=gaban_entry.era_scope,
            confidence=score,
            entry_id=gaban_entry.id,
            category=gaban_entry.category,
            tags=gaban_entry.tags,
        )

    best: tuple[float, KnowledgeEntry, str] | None = None
    for entry in GW1_KNOWLEDGE:
        for alias in entry.aliases:
            if _alias_matches(alias, message_text):
                match_scope = "message"
            elif entry.category == "quest" and _alias_matches(alias, event_text):
                match_scope = "event"
            else:
                continue

            score = entry.confidence + min(len(alias), 32) / 400.0
            if match_scope == "message":
                score += 0.24
            elif match_scope == "event":
                score -= 0.12
            if entry.era_scope == "pre_searing":
                score += _pre_searing_score(event, combined_text)
            if entry.id == "quest.scourge_beneath" and re.search(r"\btun+e?ls?\b|\btunnels?\b", message_text):
                score += 0.08
            if entry.category == "loot" and re.search(r"\b(?:drop|dropped|dye|purple|purp|pruple|rarity|item)\b", message_text):
                score += 0.16
            if entry.category == "enemy" and re.search(r"\b(?:dye|drop|dropped|purple|purp|pruple|item|rarity)\b", message_text):
                score -= 0.2
            if entry.id == "map.ascalon_city" and "fort ranik" in combined_text:
                score -= 0.18
            if best is None or score > best[0]:
                best = (min(score, 0.99), entry, alias)

    if best is None:
        return ResolvedGameContext(intent="unknown", canonical_topic="")

    score, entry, matched_alias = best
    return ResolvedGameContext(
        intent=entry.category,
        canonical_topic=entry.canonical_name,
        aliases=(matched_alias, *entry.aliases),
        response_anchors=entry.response_anchors,
        era_scope=entry.era_scope,
        confidence=score,
        entry_id=entry.id,
        category=entry.category,
        tags=entry.tags,
    )
