from __future__ import annotations

import csv
import json
import asyncio
from functools import lru_cache
from io import StringIO
from pathlib import Path
import uuid

import httpx

from app.core.config import KB_PATH
from app.models.schemas import KbSearchRequest, KnowledgeDocument, KnowledgeHit
from app.services.vector_store import VectorKnowledgeIndex


@lru_cache(maxsize=1)
def load_knowledge_base() -> list[dict]:
    if not KB_PATH.exists():
        return []
    with KB_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def clear_knowledge_base_cache() -> None:
    load_knowledge_base.cache_clear()


class KnowledgeBaseService:
    REQUIRED_COLUMNS = {"kb_type", "shop_id", "product_id", "intent_scope", "title", "content"}

    def __init__(self, vector_index: VectorKnowledgeIndex | None = None) -> None:
        self.vector_index = vector_index or VectorKnowledgeIndex()
        self.last_vector_hit_count = 0

    def list_documents(self) -> list[KnowledgeDocument]:
        return [KnowledgeDocument(**doc) for doc in load_knowledge_base()]

    def save_documents(self, documents: list[dict]) -> None:
        Path(KB_PATH).parent.mkdir(parents=True, exist_ok=True)
        with KB_PATH.open("w", encoding="utf-8") as file:
            json.dump(documents, file, ensure_ascii=False, indent=2)
        clear_knowledge_base_cache()
        self.vector_index.clear()

    async def rebuild_vector_index(self) -> dict:
        documents = load_knowledge_base()
        try:
            indexed_count = await self.vector_index.rebuild(documents)
        except (httpx.HTTPError, KeyError, ValueError):
            indexed_count = 0
        return {
            "enabled": self.vector_index.embedding_gateway.is_enabled(),
            "indexed_count": indexed_count,
            "model": self.vector_index_model_name,
        }

    @property
    def vector_index_model_name(self) -> str:
        from app.core.config import EMBEDDING_MODEL

        return EMBEDDING_MODEL

    def import_csv_text(self, csv_text: str) -> dict:
        reader = csv.DictReader(StringIO(csv_text))
        if not reader.fieldnames:
            raise ValueError("CSV file is missing a header row.")
        missing = self.REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")

        existing = load_knowledge_base()
        imported_count = 0
        skipped_count = 0
        sample_ids: list[str] = []

        for row in reader:
            if not row.get("title") or not row.get("content"):
                skipped_count += 1
                continue

            intent_scope = [item.strip() for item in row["intent_scope"].split(",") if item.strip()]
            document_id = row.get("id") or f"kb_{uuid.uuid4().hex[:12]}"
            doc = {
                "id": document_id,
                "kb_type": row["kb_type"].strip(),
                "shop_id": row["shop_id"].strip(),
                "product_id": row.get("product_id", "").strip(),
                "intent_scope": intent_scope,
                "title": row["title"].strip(),
                "content": row["content"].strip(),
                "source_name": row.get("source_name", "").strip() or None,
                "source_url": row.get("source_url", "").strip() or None,
            }
            existing.append(doc)
            imported_count += 1
            if len(sample_ids) < 5:
                sample_ids.append(document_id)

        self.save_documents(existing)
        return {
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "total_count": len(existing),
            "sample_ids": sample_ids,
        }

    async def search(self, request: KbSearchRequest) -> list[KnowledgeHit]:
        all_documents = load_knowledge_base()
        documents = self._candidate_documents(request)
        lexical_scores = {doc["id"]: self._lexical_score(doc, request) for doc in documents}
        allowed_doc_ids = {doc["id"] for doc in documents}
        vector_hits = await self.vector_index.search(all_documents, request.query, allowed_doc_ids=allowed_doc_ids)
        self.last_vector_hit_count = len(vector_hits)
        vector_scores = {hit.doc_id: hit.score for hit in vector_hits}

        results: list[KnowledgeHit] = []
        for doc in documents:
            lexical_score = lexical_scores.get(doc["id"], 0.0)
            vector_score = max(vector_scores.get(doc["id"], 0.0), 0.0)
            policy_score = self._policy_score(doc, request)
            score = lexical_score * 0.45 + vector_score * 0.45 + policy_score * 0.10

            if score <= 0.12:
                continue

            results.append(
                KnowledgeHit(
                    doc_id=doc["id"],
                    kb_type=doc["kb_type"],
                    title=doc["title"],
                    content=doc["content"],
                    score=round(score, 3),
                )
            )

        results.sort(key=lambda item: item.score, reverse=True)
        return results[:5]

    def search_sync(self, request: KbSearchRequest) -> list[KnowledgeHit]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.search(request))
        return self.search_lexical(request)

    def search_lexical(self, request: KbSearchRequest) -> list[KnowledgeHit]:
        documents = self._candidate_documents(request)
        results: list[KnowledgeHit] = []
        for doc in documents:
            score = self._lexical_score(doc, request) + self._policy_score(doc, request)
            if score <= 0.2:
                continue
            results.append(
                KnowledgeHit(
                    doc_id=doc["id"],
                    kb_type=doc["kb_type"],
                    title=doc["title"],
                    content=doc["content"],
                    score=round(score, 3),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:5]

    def _candidate_documents(self, request: KbSearchRequest) -> list[dict]:
        candidates: list[dict] = []
        for doc in load_knowledge_base():
            if doc["shop_id"] != request.shop_id:
                continue
            if doc["intent_scope"] and request.intent not in doc["intent_scope"]:
                continue
            if doc["product_id"] and request.product_id and doc["product_id"] != request.product_id:
                continue
            candidates.append(doc)
        return candidates

    def _lexical_score(self, doc: dict, request: KbSearchRequest) -> float:
        tokens = self._expand_query_tokens(request.query)
        title = doc["title"]
        body = doc["content"]
        haystack = f"{title} {body}"
        lower_haystack = haystack.lower()
        score = 0.0

        for token in tokens:
            lower_token = token.lower()
            if token in title:
                score += 0.22
            elif token in body:
                score += 0.12
            elif len(token) >= 2 and lower_token in lower_haystack:
                score += 0.08

        compact_query = "".join(request.query.split())
        if compact_query and compact_query in title:
            score += 0.35
        if compact_query and compact_query in body:
            score += 0.18
        return min(score, 1.0)

    def _policy_score(self, doc: dict, request: KbSearchRequest) -> float:
        score = 0.0
        if request.intent in doc["intent_scope"]:
            score += 0.4
        if request.product_id and doc["product_id"] == request.product_id:
            score += 0.3
        score += self._kb_type_boost(request.intent, doc["kb_type"])
        return min(score, 1.0)

    def _expand_query_tokens(self, query: str) -> list[str]:
        punctuation = "，。！？；：、,.!?;:\n\t\r"
        normalized = query
        for char in punctuation:
            normalized = normalized.replace(char, " ")

        tokens: set[str] = {token.strip() for token in normalized.split() if token.strip()}
        compact = "".join(normalized.split())
        if compact:
            tokens.add(compact)
            for size in (2, 3, 4):
                if len(compact) >= size:
                    tokens.update(compact[index : index + size] for index in range(len(compact) - size + 1))

        return sorted(tokens, key=lambda item: (-len(item), item))

    def _kb_type_boost(self, intent: str, kb_type: str) -> float:
        joined = f"{intent} {kb_type}"
        boost = 0.0
        if "FAQ" in joined or "商品" in joined:
            boost += 0.08
        if any(word in joined for word in ["物流", "发货", "shipping"]):
            boost += 0.2
        if any(word in joined for word in ["售后", "退换", "return", "after"]):
            boost += 0.2
        if any(word in joined for word in ["价格", "优惠", "price"]):
            boost += 0.14
        return boost
