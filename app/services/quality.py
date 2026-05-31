from __future__ import annotations

from typing import Any

from app.core.config import PROMISE_RISK_PATTERNS, SENSITIVE_INTENTS
from app.models.schemas import KnowledgeHit, QualityCheckResult, QualityIssue, ReplyCheckRequest


class QualityService:
    def check(self, request: ReplyCheckRequest, config: dict[str, Any] | None = None) -> QualityCheckResult:
        issues: list[QualityIssue] = []
        knowledge_hits = [KnowledgeHit.model_validate(hit) if isinstance(hit, dict) else hit for hit in request.knowledge_hits]
        runtime_config = config or {}
        patterns = runtime_config.get("promise_risk_patterns", PROMISE_RISK_PATTERNS)
        block_missing_kb = runtime_config.get("quality_block_on_sensitive_missing_kb", True)

        for pattern in patterns:
            if pattern in request.draft_reply:
                issues.append(QualityIssue(type="promise_risk", message=f"回复包含高风险承诺词: {pattern}"))

        if block_missing_kb and request.intent in SENSITIVE_INTENTS and not knowledge_hits:
            issues.append(QualityIssue(type="knowledge_missing", message="敏感场景未命中知识库，不允许直接自动回复。"))

        if request.intent in {"售后", "退换货"} and "平台审核" not in request.draft_reply and "售后入口" not in request.draft_reply:
            issues.append(QualityIssue(type="policy_guard", message="售后类回复缺少审核或售后入口引导。"))

        if request.intent == "催发货" and ("今天发" in request.draft_reply or "明天到" in request.draft_reply):
            issues.append(QualityIssue(type="timeline_risk", message="催发货回复出现具体时效承诺。"))

        if not issues:
            return QualityCheckResult(
                passed=True,
                risk_level="low",
                issues=[],
                suggestion="回复可自动发送。",
                review_mode="auto_pass",
            )

        risk_level = "medium"
        review_mode = "manual_review"
        if any(issue.type in {"promise_risk", "knowledge_missing", "timeline_risk"} for issue in issues):
            risk_level = "high"
            review_mode = "blocked"

        suggestion = "建议改为引用知识库规则的保守回复，并在必要时转人工跟进。"
        return QualityCheckResult(
            passed=False,
            risk_level=risk_level,
            issues=issues,
            suggestion=suggestion,
            review_mode=review_mode,
        )
