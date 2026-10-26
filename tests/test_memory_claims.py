"""Generated claims are reviewable candidates with their evidence (#187).

An observation extracted from a session is not a rule. It arrives as a candidate
with the claim type, a confidence, the source span and the model request that
produced it; a human promotes or rejects it, and a superseded claim keeps its own
provenance instead of being overwritten.
"""

from __future__ import annotations

import argparse
import json

import pytest

from lore.storage import (
    CLAIM_STATES,
    CLAIM_TYPES,
    claim_state,
    get_memory,
    list_memory,
    record_claim,
    set_memory_status,
    supersede_memory,
)


@pytest.fixture()
def store(tmp_path):
    return tmp_path


def _claim(store, **overrides):
    kwargs = dict(
        kind="lesson",
        title="Pin the tool version",
        body="the build breaks when the version floats",
        claim_type="observation",
        confidence=0.6,
        evidence={"session_id": "ses-a", "message_id": 4, "revision": "sha256:abc"},
        request={"provider": "ollama", "model": "llama3", "prompt_version": "v2"},
        scope_project="/repo/lore",
        source_session="ses-a",
        source_message_id=4,
    )
    kwargs.update(overrides)
    return record_claim(store, **kwargs)


# ---------------------------------------------------------------------------
# vocabulary and storage
# ---------------------------------------------------------------------------


def test_claim_vocabulary_is_closed():
    assert CLAIM_TYPES == ("observation", "hypothesis", "rule")
    assert CLAIM_STATES == ("observation", "hypothesis", "confirmed", "rejected", "superseded")


def test_a_generated_claim_is_a_candidate_by_default(store):
    memory_id = _claim(store)
    entry = get_memory(store, memory_id)
    assert entry.status == "candidate"
    assert entry.claim_state == "observation"
    assert list_memory(store, status="confirmed") == []


def test_claim_carries_type_confidence_evidence_and_request(store):
    memory_id = _claim(store, claim_type="hypothesis", confidence=0.72)
    entry = get_memory(store, memory_id)
    assert entry.claim["type"] == "hypothesis"
    assert entry.claim["confidence"] == 0.72
    assert entry.claim["evidence"]["session_id"] == "ses-a"
    assert entry.claim["evidence"]["message_id"] == 4
    assert entry.claim["request"]["model"] == "llama3"
    assert entry.claim["request"]["prompt_version"] == "v2"


def test_the_source_span_is_recorded(store):
    memory_id = _claim(store)
    entry = get_memory(store, memory_id)
    assert entry.sources
    assert entry.sources[0]["session_id"] == "ses-a"


def test_invalid_claim_type_is_rejected(store):
    with pytest.raises(ValueError, match="invalid claim_type"):
        _claim(store, claim_type="fact")


@pytest.mark.parametrize("confidence", [-0.1, 1.5, "high"])
def test_invalid_confidence_is_rejected(store, confidence):
    with pytest.raises(ValueError, match="confidence"):
        _claim(store, confidence=confidence)


def test_generation_never_confirms_by_itself(store):
    """Recording a claim must not activate a rule as a side effect."""
    memory_id = _claim(store, status="candidate")
    assert get_memory(store, memory_id).status == "candidate"


# ---------------------------------------------------------------------------
# review transitions keep the evidence
# ---------------------------------------------------------------------------


def test_accepting_promotes_a_candidate_to_confirmed(store):
    memory_id = _claim(store)
    set_memory_status(store, memory_id, "confirmed", from_status="candidate")
    entry = get_memory(store, memory_id)
    assert entry.claim_state == "confirmed"
    assert entry.claim["evidence"] is not None  # evidence survives the transition
    assert [m.id for m in list_memory(store)] == [memory_id]


def test_rejecting_keeps_the_row_and_its_evidence(store):
    memory_id = _claim(store)
    set_memory_status(store, memory_id, "rejected", from_status="candidate")
    entry = get_memory(store, memory_id)
    assert entry.status == "rejected"
    assert entry.claim_state == "rejected"
    assert entry.claim["evidence"]["message_id"] == 4


def test_supersede_keeps_the_old_claim_and_its_evidence(store):
    old_id = _claim(store)
    set_memory_status(store, old_id, "confirmed", from_status="candidate")
    new_id = supersede_memory(
        store,
        old_id,
        "Pin the tool version exactly",
        "pin it to the version the lock file records",
        metadata={"claim_type": "rule", "confidence": 0.9},
    )
    old = get_memory(store, old_id)
    assert old.claim_state == "superseded"
    assert old.superseded_by == new_id
    assert old.claim["type"] == "observation"
    assert old.claim["evidence"]["session_id"] == "ses-a"
    new = get_memory(store, new_id)
    assert new.claim_state == "confirmed"
    assert new.claim["type"] == "rule"


def test_states_stay_distinguishable(store):
    states = {}
    observation = _claim(store, title="obs")
    hypothesis = _claim(store, title="hyp", claim_type="hypothesis")
    confirmed = _claim(store, title="conf")
    set_memory_status(store, confirmed, "confirmed", from_status="candidate")
    rejected = _claim(store, title="rej")
    set_memory_status(store, rejected, "rejected", from_status="candidate")
    superseded = _claim(store, title="sup")
    set_memory_status(store, superseded, "confirmed", from_status="candidate")
    supersede_memory(store, superseded, "sup v2", "replacement")

    for name, memory_id in (
        ("observation", observation),
        ("hypothesis", hypothesis),
        ("confirmed", confirmed),
        ("rejected", rejected),
        ("superseded", superseded),
    ):
        states[name] = claim_state(get_memory(store, memory_id))
    assert len(set(states.values())) == len(states)
    assert set(states.values()) == set(CLAIM_STATES)


# ---------------------------------------------------------------------------
# audit export
# ---------------------------------------------------------------------------


def test_json_export_contains_the_evidence(store, capsys):
    import lore_cli

    memory_id = _claim(store)
    set_memory_status(store, memory_id, "confirmed", from_status="candidate")
    lore_cli.cmd_memory(
        argparse.Namespace(
            memory_action="list",
            output_dir=str(store),
            kind=None,
            project=None,
            all=False,
            limit=50,
            status="confirmed",
            json=True,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    exported = payload[0]
    assert exported["claim"]["type"] == "observation"
    assert exported["claim"]["evidence"]["session_id"] == "ses-a"
    assert exported["claim"]["request"]["prompt_version"] == "v2"
    assert exported["claim_state"] == "confirmed"
    assert exported["sources"][0]["session_id"] == "ses-a"


def test_text_listing_shows_claim_and_evidence(store, capsys):
    import lore_cli

    _claim(store, claim_type="hypothesis", confidence=0.8)
    lore_cli.cmd_memory(
        argparse.Namespace(
            memory_action="list",
            output_dir=str(store),
            kind=None,
            project=None,
            all=False,
            limit=50,
            status="candidate",
            json=False,
        )
    )
    out = capsys.readouterr().out
    assert "claim: hypothesis 0.80 · state: hypothesis" in out
    assert "evidence: session ses-a message 4" in out


def test_contradicting_later_message_becomes_a_separate_claim(store):
    """A later message does not overwrite the earlier observation."""
    first = _claim(store, title="Pins matter", body="pin the version")
    second = _claim(
        store,
        title="Floating versions are fine",
        body="the harness pins it for us",
        evidence={"session_id": "ses-b", "message_id": 9},
        source_session="ses-b",
        source_message_id=9,
    )
    first_entry = get_memory(store, first)
    second_entry = get_memory(store, second)
    assert first_entry.claim["evidence"]["session_id"] == "ses-a"
    assert second_entry.claim["evidence"]["session_id"] == "ses-b"
    assert first_entry.claim_state == "observation"
    assert second_entry.claim_state == "observation"
