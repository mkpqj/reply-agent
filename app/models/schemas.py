from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class OrderContext(BaseModel):
    status: str | None = None
    is_presale: bool = False
    logistics_status: str | None = None


class LogisticsContext(BaseModel):
    company: str | None = None
    tracking_no: str | None = None
    latest_status: str | None = None


class ChannelEventRequest(BaseModel):
    shop_id: str
    user_id: str
    content: str = Field(min_length=1)
    conversation_id: str | None = None
    product_id: str | None = None
    order_context: OrderContext | None = None
    logistics_context: LogisticsContext | None = None
    message_type: str = "text"


class IntentRecognizeRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    shop_id: str | None = None
    product_id: str | None = None
    order_context: OrderContext | None = None


class KbSearchRequest(BaseModel):
    shop_id: str
    intent: str
    query: str
    product_id: str | None = None


class ReplyGenerateRequest(BaseModel):
    intent: str
    user_message: str
    shop_id: str
    product_id: str | None = None
    conversation_history: list[str] = Field(default_factory=list)
    order_context: OrderContext | None = None
    logistics_context: LogisticsContext | None = None
    knowledge_hits: list[dict[str, Any]] = Field(default_factory=list)


class ReplyCheckRequest(BaseModel):
    intent: str
    user_message: str
    draft_reply: str
    knowledge_hits: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeHit(BaseModel):
    doc_id: str
    kb_type: str
    title: str
    content: str
    score: float


class IntentResult(BaseModel):
    intent: str
    confidence: float
    signals: list[str]
    needs_human: bool = False


class ReplyDraft(BaseModel):
    draft_reply: str
    prompt_template: str
    cited_knowledge_ids: list[str]
    risk_notes: list[str]
    model_name: str = "llm-required"


class QualityIssue(BaseModel):
    type: str
    message: str


class QualityCheckResult(BaseModel):
    passed: bool
    risk_level: Literal["low", "medium", "high"]
    issues: list[QualityIssue]
    suggestion: str
    review_mode: Literal["auto_pass", "manual_review", "blocked"]


class AgentPlanStep(BaseModel):
    step_id: str
    agent: str
    objective: str
    status: Literal["pending", "running", "completed", "skipped"] = "pending"
    observations: list[str] = Field(default_factory=list)


class AgentTraceEvent(BaseModel):
    step_id: str
    agent: str
    action: str
    output: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FollowUpTaskView(BaseModel):
    id: str
    conversation_id: str
    reason: str
    priority: str
    status: str
    assignee_id: str | None = None
    due_at: datetime
    created_at: datetime
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class ProcessedEventResponse(BaseModel):
    conversation_id: str
    message_id: str
    intent_result: IntentResult
    knowledge_hits: list[KnowledgeHit]
    reply: ReplyDraft
    quality_check: QualityCheckResult
    tags: list[str]
    action: Literal["auto_replied", "pending_review"]
    follow_up_task_id: str | None = None
    final_reply: str | None = None
    agent_plan: list[AgentPlanStep] = Field(default_factory=list)
    agent_trace: list[AgentTraceEvent] = Field(default_factory=list)


class ChatStreamEvent(BaseModel):
    type: Literal["user_message", "agent_start", "agent_chunk", "agent_done", "meta"]
    content: str | None = None
    conversation_id: str | None = None
    payload: dict[str, Any] | None = None


class ClaimTaskRequest(BaseModel):
    assignee_id: str


class ResolveTaskRequest(BaseModel):
    resolution_note: str


class ManualFollowUpReplyRequest(BaseModel):
    manual_reply: str
    resolution_note: str


class ConversationDetail(BaseModel):
    conversation: dict[str, Any]
    messages: list[dict[str, Any]]
    intents: list[dict[str, Any]]
    knowledge_hits: list[dict[str, Any]]
    replies: list[dict[str, Any]]
    quality_checks: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    follow_up_tasks: list[dict[str, Any]]


class ConversationListItem(BaseModel):
    id: str
    shop_id: str
    user_id: str
    status: str
    current_intent: str | None = None
    risk_level: str | None = None
    last_message_at: datetime
    latest_message: str | None = None
    active_tags: list[str] = Field(default_factory=list)


class DashboardMetrics(BaseModel):
    total_conversations: int
    pending_review_conversations: int
    open_follow_up_tasks: int
    claimed_follow_up_tasks: int
    high_risk_conversations: int
    auto_reply_count: int
    sent_reply_count: int
    blocked_reply_count: int


class PromptTemplateConfig(BaseModel):
    template_name: str
    instructions: str


class SystemConfig(BaseModel):
    auto_reply_enabled: bool = True
    intent_confidence_threshold: float = 0.7
    quality_block_on_sensitive_missing_kb: bool = True
    prompts: dict[str, PromptTemplateConfig]
    promise_risk_patterns: list[str]
    llm_enabled: bool = True
    llm_model: str = "gpt-4.1-mini"
    llm_api_key_configured: bool = False


class UpdateSystemConfigRequest(BaseModel):
    auto_reply_enabled: bool | None = None
    intent_confidence_threshold: float | None = None
    quality_block_on_sensitive_missing_kb: bool | None = None
    promise_risk_patterns: list[str] | None = None
    llm_enabled: bool | None = None
    llm_model: str | None = None




class KnowledgeDocument(BaseModel):
    id: str
    kb_type: str
    shop_id: str
    product_id: str = ""
    intent_scope: list[str]
    title: str
    content: str
    source_name: str | None = None
    source_url: str | None = None


class KnowledgeImportResult(BaseModel):
    imported_count: int
    skipped_count: int
    total_count: int
    sample_ids: list[str] = Field(default_factory=list)


class AgentTracePreviewRequest(ChannelEventRequest):
    conversation_history: list[str] = Field(default_factory=list)


class AgentTracePreviewResponse(BaseModel):
    intent_result: IntentResult
    knowledge_hits: list[KnowledgeHit]
    reply: ReplyDraft
    quality_check: QualityCheckResult
    should_handoff: bool
    decision_reason: str
    agent_plan: list[AgentPlanStep]
    agent_trace: list[AgentTraceEvent]


class KbVectorIndexBuildResult(BaseModel):
    enabled: bool
    indexed_count: int
    model: str
