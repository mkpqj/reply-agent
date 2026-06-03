from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import EMBEDDING_MODEL, KB_VECTOR_INDEX_PATH
from app.services.embedding_gateway import EmbeddingGateway


@dataclass
class VectorSearchHit:
    doc_id: str
    score: float


class VectorKnowledgeIndex:
    def __init__(self, embedding_gateway: EmbeddingGateway | None = None) -> None:
        self.embedding_gateway = embedding_gateway or EmbeddingGateway()

    def clear(self) -> None:
        if KB_VECTOR_INDEX_PATH.exists():
            KB_VECTOR_INDEX_PATH.unlink()

    async def rebuild(self, documents: list[dict[str, Any]]) -> int:
        self.clear()
        if not self.embedding_gateway.is_enabled() or not documents:
            return 0
        index = await self._load_or_build(documents)
        return len(index["items"]) if index else 0

    async def search(
        self,
        documents: list[dict[str, Any]],
        query: str,
        limit: int = 12,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[VectorSearchHit]:
        if not self.embedding_gateway.is_enabled() or not documents:
            return []

        try:
            index = await self._load_or_build(documents)
        except (httpx.HTTPError, KeyError, ValueError):
            # 远程 embedding 服务或缓存索引不可用时，检索应平滑降级到关键词方案。
            return []
        if not index:
            return []

        try:
            query_vectors = await self.embedding_gateway.embed_texts([query], model=index["model"])
        except (httpx.HTTPError, KeyError, ValueError):
            return []
        if not query_vectors:
            return []
        query_vector = query_vectors[0]

        hits: list[VectorSearchHit] = []
        for row in index["items"]:
            if allowed_doc_ids is not None and row["doc_id"] not in allowed_doc_ids:
                continue
            score = self._dot(query_vector, row["embedding"])
            hits.append(VectorSearchHit(doc_id=row["doc_id"], score=round(score, 6)))

        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    async def _load_or_build(self, documents: list[dict[str, Any]]) -> dict[str, Any] | None:
        signature = self._signature(documents)
        if KB_VECTOR_INDEX_PATH.exists():
            with KB_VECTOR_INDEX_PATH.open("r", encoding="utf-8") as file:
                index = json.load(file)
            if index.get("signature") == signature and index.get("model") == EMBEDDING_MODEL:
                return index

        # 签名只覆盖会影响检索结果的字段，避免无关格式变化触发不必要的重建。
        texts = [self._document_text(doc) for doc in documents]
        embeddings = await self.embedding_gateway.embed_texts(texts, model=EMBEDDING_MODEL)
        if not embeddings or len(embeddings) != len(documents):
            return None

        index = {
            "version": 1,
            "model": EMBEDDING_MODEL,
            "signature": signature,
            "items": [
                {
                    "doc_id": doc["id"],
                    "embedding": embedding,
                }
                for doc, embedding in zip(documents, embeddings, strict=True)
            ],
        }
        KB_VECTOR_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with KB_VECTOR_INDEX_PATH.open("w", encoding="utf-8") as file:
            json.dump(index, file, ensure_ascii=False)
        return index

    def _signature(self, documents: list[dict[str, Any]]) -> str:
        payload = [
            {
                "id": doc.get("id"),
                "kb_type": doc.get("kb_type"),
                "shop_id": doc.get("shop_id"),
                "product_id": doc.get("product_id"),
                "intent_scope": doc.get("intent_scope"),
                "title": doc.get("title"),
                "content": doc.get("content"),
            }
            for doc in documents
        ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _document_text(self, doc: dict[str, Any]) -> str:
        intent_scope = ", ".join(doc.get("intent_scope") or [])
        return "\n".join(
            [
                f"type: {doc.get('kb_type', '')}",
                f"shop: {doc.get('shop_id', '')}",
                f"product: {doc.get('product_id', '')}",
                f"intent: {intent_scope}",
                f"title: {doc.get('title', '')}",
                f"content: {doc.get('content', '')}",
            ]
        )

    def _dot(self, left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=False))
