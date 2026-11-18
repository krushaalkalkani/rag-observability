"""Offline tests for RRF fusion and the citation guardrail (no models, no API calls)."""

from rag.guardrails import REFUSAL, enforce_citations, split_sentences
from rag.retrieve import reciprocal_rank_fusion

CFG = {"min_word_overlap": 0.5, "require_numbers_in_source": True, "min_supported_fraction": 0.5}
CHUNK = {
    "chunk_id": "TST-2025-00001", "company": "Test Co", "ticker": "TST", "fiscal_year": 2025,
    "section": "Item 7. MD&A", "text": "Total revenue increased 15% to $478.8 billion, driven by strong iPhone sales in the U.S. market.",
}


def test_rrf_rewards_agreement_between_retrievers():
    a = [{"chunk_id": "x"}, {"chunk_id": "y"}, {"chunk_id": "z"}]
    b = [{"chunk_id": "y"}, {"chunk_id": "w"}]
    fused = [c["chunk_id"] for c in reciprocal_rank_fusion([a, b])]
    assert fused[0] == "y"  # 2nd in one list + 1st in the other beats 1st in only one list
    assert set(fused) == {"x", "y", "z", "w"}


def test_split_keeps_abbreviations_and_citations_together():
    s = split_sentences("Sales in the U.S. grew 15% [TST-2025-00001]. Costs fell. [TST-2025-00001]")
    assert s == ["Sales in the U.S. grew 15% [TST-2025-00001].", "Costs fell. [TST-2025-00001]"]


def test_supported_sentence_is_kept():
    out = enforce_citations("Total revenue increased 15% to $478.8 billion [TST-2025-00001].", [CHUNK], CFG)
    assert not out["refused"] and out["citation_coverage"] == 1.0


def test_wrong_number_is_rejected():
    out = enforce_citations("Total revenue increased 25% to $478.8 billion [TST-2025-00001].", [CHUNK], CFG)
    assert out["refused"] and out["checks"][0]["reason"].startswith("numbers not in source")


def test_uncited_and_invented_citations_are_dropped():
    answer = (
        "Total revenue increased 15% to $478.8 billion [TST-2025-00001]. "
        "Revenue was driven by strong iPhone sales [TST-2025-00001]. "
        "The company plans to double revenue by 2030."  # no citation -> dropped
    )
    out = enforce_citations(answer, [CHUNK], CFG)
    assert not out["refused"] and "2030" not in out["answer"]
    out = enforce_citations("Revenue increased 15% [FAKE-2025-00099].", [CHUNK], CFG)  # id never retrieved
    assert out["refused"] and out["answer"] == REFUSAL


def test_lead_in_lines_are_not_claims():
    out = enforce_citations("Key drivers include:\n- Revenue increased 15% [TST-2025-00001].", [CHUNK], CFG)
    assert out["citation_coverage"] == 1.0 and not out["refused"]
