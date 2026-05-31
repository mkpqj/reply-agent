from __future__ import annotations

from app.core.config import EMOTION_KEYWORDS, HIGH_RISK_KEYWORDS
from app.models.schemas import IntentResult, QualityCheckResult


class TaggingService:
    def generate_tags(self, message: str, intent_result: IntentResult, quality_result: QualityCheckResult, knowledge_hit_count: int) -> list[str]:
        tags: set[str] = set()

        if quality_result.risk_level == "high":
            tags.add("高风险会话")
        if intent_result.needs_human:
            tags.add("低置信度识别")
        if knowledge_hit_count == 0:
            tags.add("知识未命中")
        if intent_result.intent == "催发货":
            tags.add("时效敏感")
        if any(keyword in message for keyword in HIGH_RISK_KEYWORDS):
            tags.add("高风险售后")
        if any(keyword in message for keyword in EMOTION_KEYWORDS):
            tags.add("情绪激动")

        return sorted(tags)

    def needs_follow_up(self, tags: list[str], quality_result: QualityCheckResult) -> bool:
        if quality_result.review_mode == "blocked":
            return True
        return bool({"高风险会话", "低置信度识别", "知识未命中", "情绪激动"} & set(tags))

    def priority(self, tags: list[str], quality_result: QualityCheckResult) -> str:
        if quality_result.risk_level == "high" or "情绪激动" in tags:
            return "P1"
        if quality_result.risk_level == "medium" or "低置信度识别" in tags:
            return "P2"
        return "P3"
