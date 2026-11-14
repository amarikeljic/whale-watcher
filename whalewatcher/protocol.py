"""Polymarket Protocol V2 position ids.

New markets move to Protocol V2 in stages: Nov 23 and 24 2026 for crypto, weather,
finance and minor sports, Dec 1 for soccer, NFL, college football, basketball,
tennis and esports (https://docs.polymarket.com/migrate/polymarket-v2/overview.md).
A V2 market's outcome token is a *position id* instead of a CTF token id.

What a position id holds, uint256 written as 32 bytes (Polymarket/py-sdk, MIT,
``src/polymarket/_internal/protocol.py``, read 2026-11-10; this is our own code):

    byte 0        module id (1, 2 or 3)
    bytes 1..30   a key for the market (see below), bits 40..103 always zero
    byte 31       outcome index, 0 or 1

Checked against live data on 2026-11-14, on the 74 position ids Gamma already lists
for 37 open markets that still trade as v1:
* the outcome index (byte 31) matched the id's place in ``positionIds`` 74 times of 74;
* every id passed ``is_v2_position_id``;
* the market key is NOT Gamma's ``conditionId``. On the 60 ids from ordinary markets
  (module 1), bytes 1..16 are the last 16 bytes of the condition id. On the 14 from
  neg-risk markets (module 2) it is some other key. So nothing here maps an id back to a
  market: the outcome index is the only part to rely on.

A legacy CTF token id is a hash, so its bits 40..103 are effectively never all zero,
which is how the two kinds are told apart.

Nothing in the service needs this module to work: ids are matched to outcomes through
Gamma's own lists (``polymarket.outcome_ids``), and the Data API serves the condition id
and outcome index on every trade row. It is the offline check, and what an on-chain
detector would need. A real v2 market exists from 2026-11-23.
"""

from __future__ import annotations

from dataclasses import dataclass

_RESERVED = ((1 << 64) - 1) << 40
_MODULES = frozenset((1, 2, 3))
_UINT256 = (1 << 256) - 1


@dataclass(frozen=True, slots=True)
class V2Position:
    market_key: str         # "0x" + 62 hex characters (bytes 0..30): NOT Gamma's conditionId
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
    """The module, market key and outcome of a V2 YES/NO position id, or None if it is not one."""
    value = _as_int(token_id)
    if value is None or value & _RESERVED:
        return None
    text = f"{value:064x}"
    module_id, outcome_index = int(text[:2], 16), int(text[-2:], 16)
    if module_id not in _MODULES or outcome_index not in (0, 1):
        return None
    return V2Position("0x" + text[:-2], outcome_index, module_id)
