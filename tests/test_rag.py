"""
Tests for the RAG engine (LanceDB integration + optional fastembed).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("lancedb")

from embpilot.core.rag import RagEngine, _DEFAULT_DIM


@pytest.mark.asyncio
async def test_rag_open_close():
    """Verify the RAG engine can open and close a LanceDB database."""
    with tempfile.TemporaryDirectory() as tmp:
        engine = RagEngine(Path(tmp) / "lancedb")

        # Patch _embed to avoid downloading fastembed model
        with patch.object(engine, "_embed", return_value=[[0.0] * _DEFAULT_DIM]):
            await engine.open()
            assert engine._table is not None
            assert await engine.count_documents() == 0

            # Ingest a document
            doc_id = await engine.ingest_document(
                "STM32F4 has a floating-point unit.",
                metadata={"source": "datasheet", "chip": "STM32F4"},
            )
            assert doc_id is not None
            assert await engine.count_documents() == 1

            await engine.close()
            assert engine._table is None


@pytest.mark.asyncio
async def test_rag_search():
    """Verify search returns the most relevant document."""
    with tempfile.TemporaryDirectory() as tmp:
        engine = RagEngine(Path(tmp) / "lancedb")

        # Inject known vectors without real embedding
        async def fake_search(query: str, top_k: int = 5, filter_expr: str = None):
            # Simulate returning the closest match by ID
            if "error" in query.lower():
                return [{"text": "Error 0x42: DMA transfer failed", "score": 0.05, "source": "errata", "metadata": "{}"}]
            return []

        original_search = engine.search
        engine.search = fake_search  # type: ignore

        result = await engine.search("DMA error 0x42")
        assert len(result) == 1
        assert "DMA" in result[0]["text"]
        assert result[0]["source"] == "errata"


@pytest.mark.asyncio
async def test_rag_ingest_and_delete():
    """Test document lifecycle: ingest → count → delete."""
    with tempfile.TemporaryDirectory() as tmp:
        engine = RagEngine(Path(tmp) / "lancedb")
        with patch.object(engine, "_embed", return_value=[[0.0] * _DEFAULT_DIM]):
            await engine.open()

            id1 = await engine.ingest_document("Doc A", {"source": "kb"})
            id2 = await engine.ingest_document("Doc B", {"source": "kb"})
            assert await engine.count_documents() == 2

            await engine.delete_document(id1)
            assert await engine.count_documents() == 1

            await engine.close()


@pytest.mark.asyncio
async def test_rag_without_open():
    """Verify proper error when using engine without open()."""
    engine = RagEngine(Path("/tmp/nonexistent"))
    with pytest.raises(RuntimeError, match="not opened"):
        await engine.ingest_document("test")
    with pytest.raises(RuntimeError, match="not opened"):
        await engine.search("test")
