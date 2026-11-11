"""Protocol V2 position ids (layout from Polymarket's own SDK; see protocol.py)."""

from __future__ import annotations

from whalewatcher.protocol import V2Position, decode_v2_position_id, is_v2_position_id

# byte 0 = module, then the rest of the bytes31 condition id (bits 40..103 zero), last byte = outcome.
CONDITION = "0x01" + "ab" * 18 + "00" * 8 + "cdef12" + "34"
YES = int(CONDITION[2:] + "00", 16)
NO = int(CONDITION[2:] + "01", 16)
LEGACY = "70262482744164175231713225135554897410646273200471533787598889900407116102049"   # a real CTF token id


def test_a_v2_id_decodes_to_its_market_and_outcome():
    assert len(CONDITION) == 2 + 62
    assert decode_v2_position_id(str(YES)) == V2Position(CONDITION, 0, 1)
    assert decode_v2_position_id(str(NO)) == V2Position(CONDITION, 1, 1)
    assert decode_v2_position_id(hex(NO)) == V2Position(CONDITION, 1, 1), "hex and decimal spellings agree"
    assert is_v2_position_id(str(YES)) and is_v2_position_id(NO)


def test_a_legacy_ctf_token_id_is_not_a_v2_id():
    assert not is_v2_position_id(LEGACY) and decode_v2_position_id(LEGACY) is None


def test_bad_input_is_refused_not_guessed():
    assert decode_v2_position_id("") is None and decode_v2_position_id("0xc1-t0") is None
    assert not is_v2_position_id("not a number") and not is_v2_position_id(0)
    assert decode_v2_position_id(int("09" + CONDITION[4:] + "00", 16)) is None, "unknown module"
    assert decode_v2_position_id(int(CONDITION[2:] + "02", 16)) is None, "outcome must be 0 or 1"
    assert decode_v2_position_id(1 << 300) is None
