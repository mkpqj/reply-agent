from __future__ import annotations

import csv
import json
from functools import lru_cache
from io import StringIO
from pathlib import Path
import uuid

from app.core.config import KB_PATH
from app.models.schemas import KbSearchRequest, KnowledgeDocument, KnowledgeHit


@lru_cache(maxsize=1)
def load_knowledge_base() -> list[dict]:
    with KB_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def clear_knowledge_base_cache() -> None:
    load_knowledge_base.cache_clear()


class KnowledgeBaseService:
    REQUIRED_COLUMNS = {"kb_type", "shop_id", "product_id", "intent_scope", "title", "content"}

    def list_documents(self) -> list[KnowledgeDocument]:
        return [KnowledgeDocument(**doc) for doc in load_knowledge_base()]

    def save_documents(self, documents: list[dict]) -> None:
        Path(KB_PATH).parent.mkdir(parents=True, exist_ok=True)
        with KB_PATH.open("w", encoding="utf-8") as file:
            json.dump(documents, file, ensure_ascii=False, indent=2)
        clear_knowledge_base_cache()

    def import_csv_text(self, csv_text: str) -> dict:
        reader = csv.DictReader(StringIO(csv_text))
        if not reader.fieldnames:
            raise ValueError("CSV 文件缺少表头。")
        missing = self.REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV 缺少必要列: {', '.join(sorted(missing))}")

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

    def search(self, request: KbSearchRequest) -> list[KnowledgeHit]:
        documents = load_knowledge_base()
        normalized = (
            request.query.replace("？", " ")
            .replace("?", " ")
            .replace("，", " ")
            .replace(",", " ")
            .replace("。", " ")
        )
        query_tokens = [token for token in normalized.split() if token]
        if not query_tokens:
            query_tokens = [request.query[i : i + 2] for i in range(max(len(request.query) - 1, 1))]
        if request.query not in query_tokens:
            query_tokens.append(request.query)

        results: list[KnowledgeHit] = []
        for doc in documents:
            if doc["shop_id"] != request.shop_id:
                continue
            if doc["intent_scope"] and request.intent not in doc["intent_scope"]:
                continue
            if doc["product_id"] and request.product_id and doc["product_id"] != request.product_id:
                continue

            content = f'{doc["title"]} {doc["content"]}'
            score = 0.0

            for token in query_tokens:
                if token and token in content:
                    score += 0.12

            if request.intent in doc["intent_scope"]:
                score += 0.4
            if request.product_id and doc["product_id"] == request.product_id:
                score += 0.3
            if doc["kb_type"] == "商品FAQ":
                score += 0.12
            if request.intent == "催发货" and doc["kb_type"] == "物流规则":
                score += 0.24
            if request.intent in {"售后", "退换货"} and doc["kb_type"] == "售后政策":
                score += 0.24

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
        return results[:3]
