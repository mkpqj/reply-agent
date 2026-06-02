from __future__ import annotations

from typing import Any

import httpx

from app.core.config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL


class EmbeddingGateway:
    def is_enabled(self) -> bool:
        return bool(EMBEDDING_API_KEY and EMBEDDING_MODEL)

    async def embed_texts(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        if not self.is_enabled():
            return []

        payload: dict[str, Any] = {
            "model": model or EMBEDDING_MODEL,
            "input": texts,
        }
        headers = {
            "Authorization": f"Bearer {EMBEDDING_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{EMBEDDING_BASE_URL}/embeddings", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        rows = sorted(data["data"], key=lambda item: item.get("index", 0))
        return [self._normalize(row["embedding"]) for row in rows]

    def _normalize(self, vector: list[float]) -> list[float]:
        norm = sum(value * value for value in vector) ** 0.5
        if norm == 0:
            return vector
        return [value / norm for value in vector]
