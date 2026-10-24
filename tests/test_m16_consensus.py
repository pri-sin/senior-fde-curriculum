"""Module 16 regressions: quorum floor and Check validation."""
import pytest
from pydantic import ValidationError

from _labs import load

m = load("16_capstone_auditmesh")


def _vote(name, verdict, conf=0.7):
    return m.Vote(verifier=name, verdict=verdict, confidence=conf if verdict else 0.0, rationale="")


def test_single_surviving_vote_cannot_auto_accept():
    votes = [_vote("presence", None), _vote("quantitative", None), _vote("skeptic", m.Verdict.COMPLIANT)]
    assert m.consensus(votes) == (m.Verdict.INSUFFICIENT, 0.0)  # was (COMPLIANT, 1.0)


def test_two_live_votes_still_reach_a_verdict():
    votes = [_vote("presence", m.Verdict.COMPLIANT, 0.8), _vote("skeptic", m.Verdict.COMPLIANT, 0.7), _vote("quantitative", None)]
    verdict, agreement = m.consensus(votes)
    assert verdict is m.Verdict.COMPLIANT and agreement == 1.0


def test_quantitative_veto_still_wins_with_quorum():
    votes = [_vote("presence", m.Verdict.COMPLIANT, 0.9), _vote("quantitative", m.Verdict.NON_COMPLIANT, 0.95)]
    assert m.consensus(votes) == (m.Verdict.NON_COMPLIANT, 1.0)


@pytest.mark.parametrize("kind", ["max_hours", "max_months", "min_frequency_months"])
def test_quantitative_check_requires_a_param(kind):
    with pytest.raises(ValidationError):
        m.Check(type=kind)  # was accepted, then TypeError inside the verifier at runtime


def test_presence_check_needs_no_param_and_quantitative_accepts_one():
    assert m.Check(type="presence").param is None
    assert m.Check(type="max_hours", param=72).param == 72
