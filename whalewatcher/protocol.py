"""Polymarket Protocol V2 position ids.

New markets move to Protocol V2 in stages: Nov 23 and 24 2026 for crypto, weather,
finance and minor sports, Dec 1 for soccer, NFL, college football, basketball,
tennis and esports (https://docs.polymarket.com/migrate/polymarket-v2/overview.md).
A V2 market's outcome token is a *position id* instead of a CTF token id.

The layout below is the one Polymarket's own SDK uses (Polymarket/py-sdk, MIT,
``src/polymarket/_internal/protocol.py``, read 2026-11-10). This is our own code.

    position id = uint256, written as 32 bytes:
        byte 0        module id (1, 2 or 3)
        bytes 0..30   the market's bytes31 condition id (it starts with the module id)
        byte 31       outcome index, 0 or 1
        bits 40..103  always zero

A legacy CTF token id is a hash, so its bits 40..103 are effectively never all
zero, which is how the two kinds are told apart.

NOT YET CHECKED AGAINST A LIVE V2 MARKET: none exists before 2026-11-23. Nothing
in the service depends on this module yet. The Data API already serves the
condition id and outcome index on every trade row, so this is only the offline
fallback (and what an on-chain detector would need).
"""

from __future__ import annotations

from dataclasses import dataclass

_RESERVED = ((1 << 64) - 1) << 40
_MODULES = frozenset((1, 2, 3))
_UINT256 = (1 << 256) - 1


@dataclass(frozen=True, slots=True)
class V2Position:
    condition_id: str       # "0x" + 62 hex characters (bytes31)
    outcome_index: int      # 0 or 1
    module_id: int


def _as_int(token_id: str | int) -> int | None:
    if isinstance(token_id, int):
        value = token_id
    else:
        raw = str(token_id).strip()
        try:
            value = int(raw, 16) if raw[:2].lower() == "0x" else int(raw)
        except ValueError:
            return None
    return value if 0 <= value <= _UINT256 else None


def is_v2_position_id(token_id: str | int) -> bool:
    value = _as_int(token_id)
    return value is not None and value > 0 and value & _RESERVED == 0


def decode_v2_position_id(token_id: str | int) -> V2Position | None:
    """The market and outcome a V2 YES/NO position id stands for, or None if it is not one."""
    value = _as_int(token_id)
    if value is None or value & _RESERVED:
        return None
    text = f"{value:064x}"
    module_id, outcome_index = int(text[:2], 16), int(text[-2:], 16)
    if module_id not in _MODULES or outcome_index not in (0, 1):
        return None
    return V2Position("0x" + text[:-2], outcome_index, module_id)
