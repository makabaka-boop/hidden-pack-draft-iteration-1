"""Card pool for the draft: 20 unique cards (4 players x 5 cards)."""
from __future__ import annotations

CARDS: list[dict] = [
    {"id": "c01", "name": "Ember Sprite", "power": 1},
    {"id": "c02", "name": "Tide Caller", "power": 2},
    {"id": "c03", "name": "Stone Sentinel", "power": 3},
    {"id": "c04", "name": "Gale Dancer", "power": 4},
    {"id": "c05", "name": "Thorn Witch", "power": 5},
    {"id": "c06", "name": "Frost Giant", "power": 6},
    {"id": "c07", "name": "Ash Phoenix", "power": 7},
    {"id": "c08", "name": "Mire Lurker", "power": 2},
    {"id": "c09", "name": "Dawn Cleric", "power": 3},
    {"id": "c10", "name": "Night Prowler", "power": 4},
    {"id": "c11", "name": "Rune Smith", "power": 5},
    {"id": "c12", "name": "Sky Serpent", "power": 6},
    {"id": "c13", "name": "Bog Shambler", "power": 1},
    {"id": "c14", "name": "Star Acolyte", "power": 2},
    {"id": "c15", "name": "Iron Golem", "power": 7},
    {"id": "c16", "name": "Mist Weaver", "power": 3},
    {"id": "c17", "name": "Flame Herald", "power": 4},
    {"id": "c18", "name": "Root Elder", "power": 5},
    {"id": "c19", "name": "Void Imp", "power": 1},
    {"id": "c20", "name": "Storm Titan", "power": 6},
]

CARD_BY_ID = {c["id"]: c for c in CARDS}
ALL_CARD_IDS = [c["id"] for c in CARDS]


def card_public(card_id: str) -> dict:
    return dict(CARD_BY_ID[card_id])
