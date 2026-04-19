"""
Tests for the embedding service (app/services/ai/embeddings.py).

No real API calls are made — the OpenAI client is mocked at the module level
using patch. The lru_cache on _get_client is cleared between tests that need
different client behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.ai.embeddings import EmbeddingError, _get_client, embed_texts
from app.services.ingestion.chunker import chunk_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_embedding(dim: int = 1536) -> list[float]:
    """Return a deterministic dummy embedding vector."""
    return [0.01 * i for i in range(dim)]


def _make_openai_response(texts: list[str], dim: int = 1536) -> MagicMock:
    """
    Build a mock object matching the shape of openai.types.CreateEmbeddingResponse.
    response.data is a list of objects with .embedding and .index attributes.
    """
    response = MagicMock()
    response.data = [
        MagicMock(embedding=_make_fake_embedding(dim), index=i)
        for i in range(len(texts))
    ]
    return response


# ---------------------------------------------------------------------------
# embed_texts: output length matches input length
# ---------------------------------------------------------------------------

class TestEmbedTextsOutputLength:
    def test_single_text(self) -> None:
        texts = ["Lithium prices rose sharply in Q3 2024."]
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = _make_openai_response(texts)

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.openai_api_key = "sk-test"
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 64
            result = embed_texts(texts)

        assert len(result) == 1
        assert len(result[0]) == 1536

    def test_multiple_texts(self) -> None:
        texts = [f"Supply chain risk signal {i}" for i in range(5)]
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = _make_openai_response(texts)

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.openai_api_key = "sk-test"
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 64
            result = embed_texts(texts)

        assert len(result) == 5
        for vec in result:
            assert len(vec) == 1536

    def test_empty_input_returns_empty(self) -> None:
        result = embed_texts([])
        assert result == []


# ---------------------------------------------------------------------------
# embed_texts: batching
# ---------------------------------------------------------------------------

class TestEmbedTextsBatching:
    """Verify that texts are split into batches of EMBEDDING_BATCH_SIZE."""

    def test_single_batch_when_below_limit(self) -> None:
        """4 texts with batch_size=64 should produce exactly one API call."""
        texts = [f"text {i}" for i in range(4)]
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = _make_openai_response(texts)

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 64

            result = embed_texts(texts)

        mock_client.embeddings.create.assert_called_once()
        assert len(result) == 4

    def test_two_batches_when_above_limit(self) -> None:
        """
        10 texts with batch_size=4 should produce 3 API calls:
        batch [0:4], [4:8], [8:10].
        """
        texts = [f"text {i}" for i in range(10)]

        call_count = 0

        def fake_create(model, input, dimensions):
            nonlocal call_count
            call_count += 1
            return _make_openai_response(input)

        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = fake_create

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 4

            result = embed_texts(texts)

        assert call_count == 3, f"Expected 3 API calls, got {call_count}"
        assert len(result) == 10

    def test_exact_batch_boundary(self) -> None:
        """8 texts with batch_size=4 should produce exactly 2 API calls."""
        texts = [f"text {i}" for i in range(8)]

        call_count = 0

        def fake_create(model, input, dimensions):
            nonlocal call_count
            call_count += 1
            return _make_openai_response(input)

        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = fake_create

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 4

            result = embed_texts(texts)

        assert call_count == 2
        assert len(result) == 8

    def test_order_preserved_across_batches(self) -> None:
        """
        The i-th result must correspond to the i-th input text.
        Each fake embedding encodes its batch-local index in the first element.
        """
        texts = [f"text {i}" for i in range(6)]

        def fake_create(model, input, dimensions):
            resp = MagicMock()
            resp.data = [
                MagicMock(embedding=[float(i)] + [0.0] * 1535, index=i)
                for i in range(len(input))
            ]
            return resp

        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = fake_create

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 3

            result = embed_texts(texts)

        # Each batch re-indexes from 0, so result[0][0]==0, result[1][0]==1,
        # result[2][0]==2, result[3][0]==0, result[4][0]==1, result[5][0]==2.
        assert len(result) == 6
        expected_first_elements = [0.0, 1.0, 2.0, 0.0, 1.0, 2.0]
        for i, (vec, expected) in enumerate(zip(result, expected_first_elements)):
            assert vec[0] == expected, f"result[{i}][0] expected {expected}, got {vec[0]}"


# ---------------------------------------------------------------------------
# embed_texts: error handling
# ---------------------------------------------------------------------------

class TestEmbedTextsErrorHandling:
    def test_raises_embedding_error_on_api_failure(self) -> None:
        """Any exception from the OpenAI client must be wrapped in EmbeddingError."""
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = RuntimeError("connection timeout")

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 64

            with pytest.raises(EmbeddingError) as exc_info:
                embed_texts(["some text"])

        # Original exception must be chained
        assert exc_info.value.__cause__ is not None
        assert "connection timeout" in str(exc_info.value.__cause__)

    def test_embedding_error_message_includes_batch_index(self) -> None:
        """EmbeddingError message should reference the failing batch start index."""
        texts = [f"text {i}" for i in range(10)]

        call_count = 0

        def fake_create(model, input, dimensions):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("quota exceeded")
            return _make_openai_response(input)

        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = fake_create

        _get_client.cache_clear()
        with (
            patch("app.services.ai.embeddings._get_client", return_value=mock_client),
            patch("app.services.ai.embeddings.get_settings") as mock_settings,
        ):
            mock_settings.return_value.embedding_model = "text-embedding-3-small"
            mock_settings.return_value.embedding_dimensions = 1536
            mock_settings.return_value.embedding_batch_size = 4

            with pytest.raises(EmbeddingError) as exc_info:
                embed_texts(texts)

        # The second batch starts at index 4
        assert "4" in str(exc_info.value)

    def test_embedding_error_is_not_openai_specific(self) -> None:
        """EmbeddingError must be raised regardless of the underlying exception type."""
        for exc_class in (ValueError, KeyError, ConnectionError, TimeoutError):
            mock_client = MagicMock()
            mock_client.embeddings.create.side_effect = exc_class("simulated")

            _get_client.cache_clear()
            with (
                patch("app.services.ai.embeddings._get_client", return_value=mock_client),
                patch("app.services.ai.embeddings.get_settings") as mock_settings,
            ):
                mock_settings.return_value.embedding_model = "text-embedding-3-small"
                mock_settings.return_value.embedding_dimensions = 1536
                mock_settings.return_value.embedding_batch_size = 64

                with pytest.raises(EmbeddingError):
                    embed_texts(["test"])


# ---------------------------------------------------------------------------
# Chunker unit tests (no API calls)
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_short_text_returns_single_chunk(self) -> None:
        text = "Cobalt prices rose. Supply tightened."
        chunks = chunk_text(text, chunk_size=512, overlap=64)
        assert len(chunks) == 1

    def test_empty_text_returns_empty(self) -> None:
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_no_empty_chunks_in_output(self) -> None:
        text = ". ".join(["word"] * 600)
        chunks = chunk_text(text, chunk_size=100, overlap=10)
        for chunk in chunks:
            assert chunk.strip() != ""

    def test_overlap_content_present_in_next_chunk(self) -> None:
        """The last `overlap` words of chunk N should appear at the start of chunk N+1."""
        # Build a text with easily identifiable words.
        words = [f"w{i}" for i in range(200)]
        text = " ".join(words)
        chunks = chunk_text(text, chunk_size=50, overlap=10)

        if len(chunks) >= 2:
            tail_of_first = chunks[0].split()[-10:]
            head_of_second = chunks[1].split()[:10]
            assert tail_of_first == head_of_second, (
                f"Overlap mismatch: tail={tail_of_first!r}, head={head_of_second!r}"
            )

    def test_chunk_word_count_does_not_exceed_chunk_size_plus_overlap(self) -> None:
        """
        Each chunk should be at most chunk_size + overlap words (the extra comes
        from the overlap carry-forward seed).
        """
        text = ". ".join([f"sentence number {i} has some extra words in it" for i in range(50)])
        chunk_size, overlap = 30, 5
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        for chunk in chunks:
            word_count = len(chunk.split())
            assert word_count <= chunk_size + overlap, (
                f"Chunk too long: {word_count} words (limit={chunk_size + overlap})"
            )

    def test_overlap_must_be_less_than_chunk_size(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            chunk_text("some text", chunk_size=10, overlap=10)

    def test_total_coverage_no_words_dropped(self) -> None:
        """
        Every word in the original text must appear in at least one chunk.
        (Overlap means some words appear in multiple chunks — that is correct.)
        """
        words = [f"token{i}" for i in range(300)]
        text = " ".join(words)
        chunks = chunk_text(text, chunk_size=80, overlap=15)
        covered = set()
        for chunk in chunks:
            for word in chunk.split():
                covered.add(word)
        for word in words:
            assert word in covered, f"{word!r} was dropped from chunking output"
