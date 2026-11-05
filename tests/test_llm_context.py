from unittest.mock import Mock
import pytest
from myUtils.llm_context import ContextPreparationError, compact_repeated_lines, prepare_transcript_context, split_utf8


def test_chunks_cover_utf8_source_without_loss():
    text = ("逐字稿🙂\n" * 1000) + ("a" * 10000)
    chunks = split_utf8(text, 256)
    assert "".join(chunks) == text
    assert all(len(chunk.encode()) <= 256 for chunk in chunks)


def test_compacts_only_context_and_reuses_summary():
    assert compact_repeated_lines("same\nsame\nnext\nsame") == "same\nnext\nsame"
    text = "".join(f"Line {index} 有內容\n" for index in range(3000))
    context = {"transcriptText": text, "imageLocalPaths": ["keep.jpg"]}
    summarize = Mock(return_value="Verified factual summary")
    result = prepare_transcript_context(context, summarize, budget=4000)
    assert 1 < summarize.call_count <= 32
    assert len(result["transcriptText"].encode()) <= 4000
    assert context["transcriptText"] == text
    assert result["imageLocalPaths"] == ["keep.jpg"]
    count = summarize.call_count
    assert prepare_transcript_context(context, summarize, budget=4000)["transcriptText"] == result["transcriptText"]
    assert summarize.call_count == count


def test_overlarge_input_or_summary_fails_without_loop():
    summarize = Mock(return_value="summary")
    with pytest.raises(ContextPreparationError, match="batch"):
        prepare_transcript_context({"transcriptText": "x" * 40000}, summarize, budget=256)
    summarize.assert_not_called()
    with pytest.raises(ContextPreparationError, match="oversized"):
        prepare_transcript_context({"transcriptText": "x" * 2000}, lambda *_: "z" * 2001, budget=1000)
