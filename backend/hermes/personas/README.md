# Local Persona Memory Files

This directory is for private, local persona notes that shape Hermes replies.
The actual `*.md` memory files are intentionally ignored by git because they may contain player names,
relationship details, play history, preferences, or other private context.

Hermes will load these optional files when present:

- `<persona>.md`: stable character voice, boundaries, personality, and roleplay style.
- `<persona>.lore.md`: local world knowledge the character can treat as lived context.
- `<persona>.memory.md`: durable relationship or session continuity for that specific player and character.

When `HERMES_PERSONA_FILE_MEMORY_ENABLED=true` (default), Hermes may append safe, durable play memories
to these ignored local files after its normal memory flush:

- ordinary relationship/session continuity goes to `<persona>.memory.md`
- explicit character-development or backstory notes go to `<persona>.md`

This is meant to make companions evolve with play while keeping private/player-specific details out of git.
Set `HERMES_PERSONA_FILE_MEMORY_ENABLED=false` to disable local file updates.

For example, a local Azele setup may use:

```text
azele.md
azele.lore.md
azele.memory.md
```

Keep these files on your own machine. Do not commit them.

## Suggested Templates

### `<persona>.md`

```md
# <Character> living character notes

- Current era/location:
- Character identity:
- Voice and tone:
- Conversation style:
- Boundaries:
- Growth rule: add only things the character actually experiences in play.
```

### `<persona>.lore.md`

```md
# <Character> world memory notes

- What the character knows about the current world/time period:
- Places the character knows personally:
- NPCs, factions, enemies, and local tensions:
- Things the character should not know:
- How to use lore in conversation:
```

### `<persona>.memory.md`

```md
# <Character> personal memory notes

## About the player

- The player is the person the character is traveling with.
- The character should address the player directly as "you".
- Add stable player preferences only when explicitly stated or clearly important.

## Shared continuity

- Notable conversations:
- Rare drops or meaningful discoveries:
- Recurring jokes:
- Quest or mission moments worth remembering:
```
