"""Protocol V2 position ids (layout from Polymarket's own SDK, checked on live ids; see protocol.py)."""

from __future__ import annotations

from whalewatcher.protocol import V2Position, decode_v2_position_id, is_v2_position_id

# byte 0 = module, then the market key (bits 40..103 zero), last byte = outcome.
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


# Read from Gamma on 2026-11-14: an open market that still trades as v1 but already lists
# its positionIds, and one from a neg-risk market.
LIVE_CONDITION = "0x41f94101d57bf028eff62f2cc3714adb79722553dc59c1676071459c0b009e45"
LIVE_IDS = ["666889148854578891742619935283574117032411625920935004001936672821144977408",
            "666889148854578891742619935283574117032411625920935004001936672821144977409"]
LIVE_NEG_RISK = "1109911123776083898454459615780640993361842614811275108552557309616508108800"


def test_live_ids_decode_to_their_place_in_the_list():
    for index, pid in enumerate(LIVE_IDS):
        d = decode_v2_position_id(pid)
        assert d is not None and d.outcome_index == index and d.module_id == 1
    assert decode_v2_position_id(LIVE_NEG_RISK).module_id == 2


def test_the_key_inside_is_not_the_condition_id():
    key = decode_v2_position_id(LIVE_IDS[0]).market_key
    assert key != LIVE_CONDITION, "the id cannot be mapped back to Gamma's conditionId"
    assert key[4:36] == LIVE_CONDITION[34:], "module 1 holds the last 16 bytes of it"
