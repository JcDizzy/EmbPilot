"""
Local RAG engine — vector search over embedded documentation (Datasheets,
SDK Error Code manuals, Troubleshooting KB) using fastembed + LanceDB.

Architecture
------------
User imports documents (datasheet sections, error-code lookups, KB articles)
via tools or REST.  The engine chunks, embeds (fastembed), and stores them
in a local LanceDB table.  When the MCP server detects repeated errors,
it automatically retrieves relevant knowledge via semantic search and
injects it into the AI context to eliminate hallucinations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import lancedb
import pandas as pd
import pyarrow as pa

logger = logging.getLogger(__name__)

# Default embedding dimension for fastembed's "BAAI/bge-small-en-v1.5"
_DEFAULT_DIM = 384


class RagEngine:
    """Thin wrapper around fastembed + LanceDB for semantic retrieval.

    Parameters
    ----------
    db_path:
        File-system path for the LanceDB database directory.
    embedding_model:
        Name of the fastembed model to use (default: ``"BAAI/bge-small-en-v1.5"``).
    """

    def __init__(
        self,
        db_path: Path,
        embedding_model: str = "BAAI/bge-small-en-v1.5",
    ) -> None:
        self._db_path = db_path
        self._model_name = embedding_model
        self._embedder: Any = None  # lazy init
        self._table: Any = None     # lazy init

    # ── Lifecycle ────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open (or create) the LanceDB database and ensure the ``docs`` table."""
        db_path_str = str(self._db_path)
        db = await lancedb.connect_async(db_path_str)
        try:
            self._table = await db.open_table("docs")
        except Exception:
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("source", pa.string()),
                pa.field("metadata", pa.string()),   # JSON blob
                pa.field("vector", pa.list_(pa.float32(), _DEFAULT_DIM)),
            ])
            self._table = await db.create_table("docs", schema=schema)
        logger.info("RAG engine ready | path=%s | model=%s", self._db_path, self._model_name)

    async def close(self) -> None:
        self._table = None
        self._embedder = None
        logger.info("RAG engine closed")

    # ── Embedding ────────────────────────────────────────────────────

    def _get_embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=self._model_name)
        return self._embedder

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        embedder = self._get_embedder()
        return [list(vec) for vec in embedder.passage_embed(texts)]

    # ── Ingestion ────────────────────────────────────────────────────

    async def ingest_document(
        self,
        text: str,
        metadata: Optional[dict[str, Any]] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Embed and store a document chunk.

        Parameters
        ----------
        text:
            Raw text of the document (e.g., a datasheet section).
        metadata:
            Optional dict (e.g., ``{"source": "datasheet", "chip": "STM32F4"}``).
        doc_id:
            Optional unique ID; auto-generated if omitted.

        Returns
        -------
        The document ID.
        """
        import json
        import uuid

        if self._table is None:
            raise RuntimeError("RAG engine not opened — call open() first")

        doc_id = doc_id or uuid.uuid4().hex[:16]
        vector = self._embed([text])[0]
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        df = pd.DataFrame([{
            "id": doc_id,
            "text": text,
            "source": (metadata or {}).get("source", "unknown"),
            "metadata": metadata_json,
            "vector": vector,
        }])
        await self._table.add(df)
        logger.debug("Ingested document %s (%d chars)", doc_id, len(text))
        return doc_id

    # ── Search ───────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filter_expr: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Retrieve the *top_k* most relevant document chunks.

        Parameters
        ----------
        query:
            Natural-language query string.
        top_k:
            Number of results to return.
        filter_expr:
            Optional LanceDB filter expression, e.g. ``"source = 'datasheet'"``.

        Returns
        -------
        A list of dicts with keys ``text``, ``score``, ``source``, ``metadata``.
        """
        if self._table is None:
            raise RuntimeError("RAG engine not opened — call open() first")

        query_vec = self._embed([query])[0]

        search_builder = self._table.search(query_vec).limit(top_k)
        if filter_expr:
            search_builder = search_builder.where(filter_expr)

        results = await search_builder.to_list()

        return [
            {
                "text": r["text"],
                "score": float(r.get("_distance", 0)),
                "source": r.get("source", ""),
                "metadata": r.get("metadata", "{}"),
            }
            for r in results
        ]

    # ── Management ───────────────────────────────────────────────────

    async def count_documents(self) -> int:
        """Return the number of stored documents."""
        if self._table is None:
            return 0
        return await self._table.count_rows()

    async def delete_document(self, doc_id: str) -> None:
        """Delete a document by ID."""
        if self._table is not None:
            await self._table.delete(f"id = '{doc_id}'")

    async def list_sources(self) -> list[str]:
        """Return distinct source values."""
        if self._table is None:
            return []
        result = await self._table.search().limit(1).to_list()
        # LanceDB doesn't have a native distinct; defer to count.
        return list(set(r.get("source", "") for r in result))
