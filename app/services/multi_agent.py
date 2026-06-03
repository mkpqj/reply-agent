from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.models.schemas import (
    AgentPlanStep,
    AgentTraceEvent,
    ChannelEventRequest,
    IntentRecognizeRequest,
    IntentResult,
    KbSearchRequest,
    KnowledgeHit,
    QualityCheckResult,
    ReplyCheckRequest,
    ReplyDraft,
    ReplyGenerateRequest,
)
from app.services.intent import IntentService
from app.services.intent import is_greeting_message
from app.services.knowledge_base import KnowledgeBaseService
from app.services.quality import QualityService
from app.services.reply import ReplyService
from app.services.tagging import TaggingService


def human_handoff_template_for_intent(intent: str) -> str:
    return (
        "您好，您的问题已收到。当前还需要人工客服进一步核实后处理，"
        "我已为您转入待跟进队列，请稍后留意客服回复。感谢您的理解。"
    )


@dataclass
class MultiAgentRunResult:
    intent_result: IntentResult
    knowledge_hits: list[KnowledgeHit]
    reply: ReplyDraft
    quality_check: QualityCheckResult
    tags: list[str]
    action: Literal["auto_replied", "pending_review"]
    final_reply: str | None
    reply_status: str
    conversation_status: str
    risk_level: str
    follow_up_reason: str | None
    follow_up_priority: str | None
    agent_plan: list[AgentPlanStep]
    agent_trace: list[AgentTraceEvent]
    should_handoff: bool
    decision_reason: str


AgentName = Literal[
    "intent_agent",
    "retrieval_agent",
    "customer_service_agent",
    "quality_agent",
    "tagging_agent",
    "escalation_agent",
    "delivery_agent",
    "__end__",
]


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    role: str
    instructions: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    can_handoff_to: tuple[str, ...] = field(default_factory=tuple)
    model: str | None = None
    uses_llm: bool = False


class CustomerSupportGraphState(TypedDict, total=False):
    # LangGraph 节点之间传递尽量保持可序列化的普通值；service 只作为依赖注入
    # 放在 state 中，最终对外结果会在 Runtime 边界重新组装成 Pydantic 模型。
    messages: Annotated[list[AnyMessage], add_messages]
    request: ChannelEventRequest
    conversation_id: str
    history: list[str]
    runtime_config: dict[str, Any]
    services: dict[str, Any]
    intent_result: dict[str, Any]
    knowledge_hits: list[dict[str, Any]]
    reply: dict[str, Any]
    quality_check: dict[str, Any]
    tags: list[str]
    should_handoff: bool
    decision_reason: str
    action: str
    final_reply: str | None
    reply_status: str
    conversation_status: str
    risk_level: str
    follow_up_reason: str | None
    follow_up_priority: str | None
    agent_plan: list[AgentPlanStep]
    agent_trace: list[AgentTraceEvent]
    retrieval_metadata: dict[str, Any]
    next_agent: AgentName


AGENT_DEFINITIONS: dict[str, AgentDefinition] = {
    "supervisor_agent": AgentDefinition(
        name="supervisor_agent",
        role="Workflow supervisor",
        instructions=(
            "Plan the customer-service workflow and hand off to the next role agent. "
            "Do not draft customer-facing text."
        ),
        can_handoff_to=(
            "intent_agent",
            "retrieval_agent",
            "customer_service_agent",
            "quality_agent",
            "tagging_agent",
            "escalation_agent",
            "delivery_agent",
        ),
        uses_llm=False,
    ),
    "intent_agent": AgentDefinition(
        name="intent_agent",
        role="Intent triage agent",
        instructions="Classify the customer intent, confidence, and need for human review.",
        tools=("IntentService.recognize",),
        can_handoff_to=("retrieval_agent",),
        uses_llm=False,
    ),
    "retrieval_agent": AgentDefinition(
        name="retrieval_agent",
        role="Knowledge retrieval agent",
        instructions="Retrieve grounded merchant knowledge. Never invent policy or product facts.",
        tools=("KnowledgeBaseService.search", "VectorKnowledgeIndex.search"),
        can_handoff_to=("customer_service_agent",),
        uses_llm=False,
    ),
    "customer_service_agent": AgentDefinition(
        name="customer_service_agent",
        role="Customer reply agent",
        instructions=(
            "Draft a concise customer-facing reply from the recognized intent, conversation "
            "context, and retrieved evidence."
        ),
        tools=("ReplyService.generate",),
        can_handoff_to=("quality_agent",),
        model="runtime.llm_model",
        uses_llm=True,
    ),
    "quality_agent": AgentDefinition(
        name="quality_agent",
        role="Quality and guardrail agent",
        instructions="Check promise risk, missing knowledge, policy boundaries, and review mode.",
        tools=("QualityService.check",),
        can_handoff_to=("tagging_agent", "customer_service_agent", "escalation_agent"),
        uses_llm=False,
    ),
    "tagging_agent": AgentDefinition(
        name="tagging_agent",
        role="Operations tagging agent",
        instructions="Generate risk and operations tags for dashboards and queues.",
        tools=("TaggingService.generate_tags",),
        can_handoff_to=("escalation_agent",),
        uses_llm=False,
    ),
    "escalation_agent": AgentDefinition(
        name="escalation_agent",
        role="Human handoff decision agent",
        instructions="Decide whether the conversation can auto-reply or needs human follow-up.",
        tools=("TaggingService.needs_follow_up", "TaggingService.priority"),
        can_handoff_to=("delivery_agent",),
        uses_llm=False,
    ),
    "delivery_agent": AgentDefinition(
        name="delivery_agent",
        role="Response delivery agent",
        instructions="Prepare final reply status, handoff template, and follow-up task metadata.",
        tools=("human_handoff_template_for_intent",),
        can_handoff_to=(),
        uses_llm=False,
    ),
}


ROLE_SEQUENCE: list[tuple[str, str, str, str]] = [
    (
        "recognize_intent",
        "intent_agent",
        "识别客户意图、置信度和是否需要人工判断。",
        "intent_triage",
    ),
    (
        "search_knowledge",
        "retrieval_agent",
        "按意图、商品和对话上下文检索商家知识库证据。",
        "knowledge_retrieval",
    ),
    (
        "draft_reply",
        "customer_service_agent",
        "基于检索证据和客服话术约束生成面向客户的回复草稿。",
        "reply_drafting",
    ),
    (
        "check_reply",
        "quality_agent",
        "检查回复是否存在承诺、政策、知识缺失和敏感场景风险。",
        "quality_gate",
    ),
    (
        "tag_conversation",
        "tagging_agent",
        "生成运营标签，用于风险看板和待跟进队列筛选。",
        "ops_tagging",
    ),
    (
        "route_conversation",
        "escalation_agent",
        "结合意图、质检、标签和系统配置决定自动回复或升级人工。",
        "handoff_routing",
    ),
    (
        "prepare_response_delivery",
        "delivery_agent",
        "准备最终发送内容、回复状态和人工跟进任务元数据。",
        "delivery_preparation",
    ),
]

# supervisor 通过这两个映射在“可展示的计划步骤”和“可执行的图节点”之间转换。
STEP_TO_AGENT = {step_id: agent for step_id, agent, _, _ in ROLE_SEQUENCE}
AGENT_TO_STEP = {agent: step_id for step_id, agent, _, _ in ROLE_SEQUENCE}


def _require_state_value(state: CustomerSupportGraphState, key: str, dependency: str) -> Any:
    value = state.get(key)
    if value is None:
        raise RuntimeError(f"Agent requires `{dependency}` to run first.")
    return value


def _history_query(message: str, history: list[str]) -> str:
    # 检索时补一点最近上下文，但如果当前消息已在历史里，就避免重复拼接。
    useful_history = [item for item in history[-3:] if item and item != message]
    if not useful_history:
        return message
    return " ".join([*useful_history, message])


def _merge_hits(primary: list[KnowledgeHit], secondary: list[KnowledgeHit]) -> list[KnowledgeHit]:
    # 当前消息检索和上下文扩展检索可能命中同一文档；同一 doc_id 只保留最高分。
    merged: dict[str, KnowledgeHit] = {item.doc_id: item for item in primary}
    for item in secondary:
        existing = merged.get(item.doc_id)
        if not existing or item.score > existing.score:
            merged[item.doc_id] = item
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:5]


def _result_metadata_for_step(step_id: str, state: CustomerSupportGraphState, payload: Any) -> Any:
    if step_id == "search_knowledge":
        return {
            "hits": payload,
            "retrieval": state.get("retrieval_metadata", {}),
        }
    return payload


def _append_trace(
    state: CustomerSupportGraphState,
    *,
    step_id: str,
    agent: str,
    action: str,
    output: str,
    metadata: dict[str, Any] | None = None,
) -> list[AgentTraceEvent]:
    trace = list(state.get("agent_trace", []))
    trace.append(
        AgentTraceEvent(
            step_id=step_id,
            agent=agent,
            action=action,
            output=output,
            metadata=metadata or {},
        )
    )
    return trace


def _append_agent_trace(
    state: CustomerSupportGraphState,
    *,
    step_id: str,
    output: str,
    arguments: dict[str, Any],
    payload: Any,
) -> list[AgentTraceEvent]:
    agent_name = STEP_TO_AGENT[step_id]
    definition = AGENT_DEFINITIONS[agent_name]
    return _append_trace(
        state,
        step_id=step_id,
        agent=agent_name,
        action="role_execute",
        output=output,
        metadata={
            "role": definition.role,
            "instructions": definition.instructions,
            "tools": list(definition.tools),
            "can_handoff_to": list(definition.can_handoff_to),
            "model": definition.model,
            "uses_llm": definition.uses_llm,
            "arguments": arguments,
            "result": _result_metadata_for_step(step_id, state, payload),
        },
    )


def _mark_plan_step(
    state: CustomerSupportGraphState,
    step_id: str,
    status: Literal["completed", "running", "skipped"],
    observations: list[str] | None = None,
) -> list[AgentPlanStep]:
    plan = [step.model_copy(deep=True) for step in state.get("agent_plan", [])]
    for step in plan:
        if step.step_id == step_id:
            step.status = status
            if observations:
                step.observations.extend(observations)
            break
    return plan


def _supervisor_observations(state: CustomerSupportGraphState) -> list[str]:
    observations: list[str] = []
    request = state["request"]
    if state.get("history"):
        observations.append(f"Use {min(len(state['history']), 6)} recent turns as conversation memory.")
    if request.order_context:
        observations.append("Order context is available for policy-sensitive decisions.")
    if request.logistics_context:
        observations.append("Logistics context is available for shipping-sensitive decisions.")
    if request.product_id:
        observations.append(f"Prefer product-scoped retrieval for product_id={request.product_id}.")
    return observations


def _build_role_plan(state: CustomerSupportGraphState) -> list[AgentPlanStep]:
    observations = _supervisor_observations(state)
    return [
        AgentPlanStep(
            step_id=step_id,
            agent=agent,
            objective=f"{objective} Role: {AGENT_DEFINITIONS[agent].role}.",
            observations=observations if index == 0 else [],
        )
        for index, (step_id, agent, objective, _) in enumerate(ROLE_SEQUENCE)
    ]


def _next_agent(state: CustomerSupportGraphState) -> AgentName:
    if not state.get("intent_result"):
        return "intent_agent"
    if state.get("knowledge_hits") is None:
        return "retrieval_agent"
    if not state.get("reply"):
        return "customer_service_agent"
    if not state.get("quality_check"):
        return "quality_agent"
    if state.get("tags") is None:
        return "tagging_agent"
    if state.get("should_handoff") is None:
        return "escalation_agent"
    if state.get("action") is None:
        return "delivery_agent"
    return "__end__"


class SupervisorAgent:
    name = "supervisor_agent"

    def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        working_state = state

        if not state.get("agent_plan"):
            # 计划阶段保持确定性，方便前端先展示完整路线，再逐步填充各角色结果。
            plan = _build_role_plan(state)
            trace = _append_trace(
                state,
                step_id="supervisor_plan",
                agent=self.name,
                action="plan_roles",
                output=f"planned {len(plan)} role agents",
                metadata={
                    "framework": "langgraph",
                    "architecture": "supervisor_multi_agent",
                    "roles": [agent for _, agent, _, _ in ROLE_SEQUENCE],
                    "agent_definitions": {
                        name: {
                            "role": definition.role,
                            "tools": list(definition.tools),
                            "can_handoff_to": list(definition.can_handoff_to),
                            "model": definition.model,
                            "uses_llm": definition.uses_llm,
                        }
                        for name, definition in AGENT_DEFINITIONS.items()
                    },
                    "planner": "deterministic_supervisor",
                },
            )
            updates["agent_plan"] = plan
            updates["agent_trace"] = trace
            working_state = {**state, **updates}

        next_agent = _next_agent(working_state)
        updates["next_agent"] = next_agent
        if next_agent == "__end__":
            updates["agent_trace"] = _append_trace(
                working_state,
                step_id="supervisor_complete",
                agent=self.name,
                action="complete",
                output="all role agents completed",
                metadata={"final_action": working_state.get("action")},
            )
            return updates

        step_id = AGENT_TO_STEP[next_agent]
        updates["agent_plan"] = _mark_plan_step(working_state, step_id, "running")
        route_trace_state = {**working_state, "agent_plan": updates["agent_plan"]}
        updates["agent_trace"] = _append_trace(
            route_trace_state,
            step_id=f"supervisor_route_{step_id}",
            agent=self.name,
            action="route",
            output=f"handoff to {next_agent}",
            metadata={
                "next_agent": next_agent,
                "step_id": step_id,
                "handoff_allowed": next_agent in AGENT_DEFINITIONS[self.name].can_handoff_to,
                "agent_definition": {
                    "role": AGENT_DEFINITIONS[next_agent].role,
                    "tools": list(AGENT_DEFINITIONS[next_agent].tools),
                    "model": AGENT_DEFINITIONS[next_agent].model,
                    "uses_llm": AGENT_DEFINITIONS[next_agent].uses_llm,
                },
            },
        )
        return updates

    def route(self, state: CustomerSupportGraphState) -> AgentName:
        return state.get("next_agent", "__end__")


class IntentAgent:
    name = "intent_agent"
    step_id = "recognize_intent"

    def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        request = state["request"]
        intent_service: IntentService = state["services"]["intent"]
        kb_service: KnowledgeBaseService = state["services"]["kb"]
        confidence_threshold = state.get("runtime_config", {}).get("intent_confidence_threshold", 0.7)
        result = intent_service.recognize(
            IntentRecognizeRequest(
                conversation_id=state["conversation_id"],
                message=request.content,
                shop_id=request.shop_id,
                product_id=request.product_id,
                order_context=request.order_context,
            )
        )
        if (
            result.intent == "其他"
            and not request.order_context
            and kb_service.has_product_faq(request.shop_id, request.product_id)
            and kb_service.looks_like_product_question(request.shop_id, request.product_id, request.content)
        ):
            result.intent = "售前咨询"
            result.confidence = max(result.confidence, 0.82)
            if "命中当前店铺商品知识，按商品咨询处理" not in result.signals:
                result.signals.append("命中当前店铺商品知识，按商品咨询处理")
        result.needs_human = result.confidence < confidence_threshold
        payload = result.model_dump()
        output = f"intent={result.intent}, confidence={result.confidence}, needs_human={result.needs_human}"
        return {
            "intent_result": payload,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state,
                step_id=self.step_id,
                output=output,
                arguments={"confidence_threshold": confidence_threshold},
                payload=payload,
            ),
        }


class RetrievalAgent:
    name = "retrieval_agent"
    step_id = "search_knowledge"

    async def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        request = state["request"]
        intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "intent_agent"))
        kb_service: KnowledgeBaseService = state["services"]["kb"]
        hits = await kb_service.search(
            KbSearchRequest(
                shop_id=request.shop_id,
                intent=intent_result.intent,
                query=request.content,
                product_id=request.product_id,
            )
        )

        expanded_query = _history_query(request.content, state.get("history", []))
        if expanded_query and expanded_query != request.content:
            # 对“那物流呢？”这类短追问，再用最近上下文检索一次，
            # 既补足语义，又不改写用户原始消息。
            extra_hits = await kb_service.search(
                KbSearchRequest(
                    shop_id=request.shop_id,
                    intent=intent_result.intent,
                    query=expanded_query,
                    product_id=request.product_id,
                )
            )
            hits = _merge_hits(hits, extra_hits)

        payload = [item.model_dump() for item in hits]
        retrieval_metadata = {
            "query": request.content,
            "expanded_query": expanded_query or request.content,
            "doc_ids": [item.doc_id for item in hits],
            "scores": [item.score for item in hits],
            "embedding_enabled": kb_service.vector_index.embedding_gateway.is_enabled(),
            "vector_hits": kb_service.last_vector_hit_count,
        }
        state_with_retrieval = {**state, "retrieval_metadata": retrieval_metadata}
        output = f"returned {len(hits)} knowledge items"
        return {
            "knowledge_hits": payload,
            "retrieval_metadata": retrieval_metadata,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state_with_retrieval,
                step_id=self.step_id,
                output=output,
                arguments={
                    "intent": intent_result.intent,
                    "query": request.content,
                    "product_id": request.product_id,
                },
                payload=payload,
            ),
        }


class CustomerServiceAgent:
    name = "customer_service_agent"
    step_id = "draft_reply"

    async def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        request = state["request"]
        intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "intent_agent"))
        reply_service: ReplyService = state["services"]["reply"]
        knowledge_hits = _require_state_value(state, "knowledge_hits", "retrieval_agent")
        reply = await reply_service.generate(
            ReplyGenerateRequest(
                intent=intent_result.intent,
                user_message=request.content,
                shop_id=request.shop_id,
                product_id=request.product_id,
                conversation_history=state.get("history", []),
                order_context=request.order_context,
                logistics_context=request.logistics_context,
                knowledge_hits=knowledge_hits,
            ),
            prompt_overrides=state.get("runtime_config", {}).get("prompts"),
            runtime_config=state.get("runtime_config", {}),
        )
        payload = reply.model_dump()
        output = f"drafted reply with {len(reply.cited_knowledge_ids)} citations via {reply.model_name}"
        return {
            "reply": payload,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state,
                step_id=self.step_id,
                output=output,
                arguments={"intent": intent_result.intent},
                payload=payload,
            ),
        }


class QualityAgent:
    name = "quality_agent"
    step_id = "check_reply"

    def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        request = state["request"]
        intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "intent_agent"))
        quality_service: QualityService = state["services"]["quality"]
        reply = ReplyDraft.model_validate(_require_state_value(state, "reply", "customer_service_agent"))
        knowledge_hits = _require_state_value(state, "knowledge_hits", "retrieval_agent")
        quality_check = quality_service.check(
            ReplyCheckRequest(
                intent=intent_result.intent,
                user_message=request.content,
                draft_reply=reply.draft_reply,
                knowledge_hits=knowledge_hits,
            ),
            config=state.get("runtime_config", {}),
        )
        payload = quality_check.model_dump()
        output = (
            f"passed={quality_check.passed}, risk={quality_check.risk_level}, "
            f"review_mode={quality_check.review_mode}"
        )
        return {
            "quality_check": payload,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state,
                step_id=self.step_id,
                output=output,
                arguments={"intent": intent_result.intent},
                payload=payload,
            ),
        }


class TaggingAgent:
    name = "tagging_agent"
    step_id = "tag_conversation"

    def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        request = state["request"]
        tagging_service: TaggingService = state["services"]["tagging"]
        intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "intent_agent"))
        quality_check = QualityCheckResult.model_validate(_require_state_value(state, "quality_check", "quality_agent"))
        knowledge_hits = _require_state_value(state, "knowledge_hits", "retrieval_agent")

        tags = tagging_service.generate_tags(
            message=request.content,
            intent_result=intent_result,
            quality_result=quality_check,
            knowledge_hit_count=len(knowledge_hits),
        )
        if intent_result.intent == "其他" and is_greeting_message(request.content):
            tags = [tag for tag in tags if tag not in {"低置信度识别", "知识未命中"}]

        payload = {"tags": tags}
        output = f"generated {len(tags)} tags"
        return {
            "tags": tags,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state,
                step_id=self.step_id,
                output=output,
                arguments={},
                payload=payload,
            ),
        }


class EscalationAgent:
    name = "escalation_agent"
    step_id = "route_conversation"

    def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "intent_agent"))
        quality_check = QualityCheckResult.model_validate(_require_state_value(state, "quality_check", "quality_agent"))
        tagging_service: TaggingService = state["services"]["tagging"]
        tags = list(_require_state_value(state, "tags", "tagging_agent"))
        runtime_config = state.get("runtime_config", {})
        reasons: list[str] = []
        if intent_result.needs_human:
            reasons.append("intent_confidence_below_threshold")
        if not quality_check.passed:
            reasons.append(f"quality_{quality_check.review_mode}")
        if not runtime_config.get("auto_reply_enabled", True):
            reasons.append("auto_reply_disabled")
        if tagging_service.needs_follow_up(tags, quality_check):
            reasons.append("tagging_requires_follow_up")
        if intent_result.intent == "其他" and is_greeting_message(state["request"].content):
            # 问候语本身信息量低，但通常不需要因此进入人工队列。
            reasons = [reason for reason in reasons if reason != "tagging_requires_follow_up"]

        should_handoff = bool(reasons)
        decision_reason = ", ".join(reasons) if reasons else "all_agents_green"
        payload = {
            "decision": "human_follow_up" if should_handoff else "auto_reply",
            "reason": decision_reason,
            "reasons": reasons,
        }
        output = f"decision={payload['decision']}; reason={decision_reason}"
        return {
            "should_handoff": should_handoff,
            "decision_reason": decision_reason,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state,
                step_id=self.step_id,
                output=output,
                arguments={},
                payload=payload,
            ),
        }


class DeliveryAgent:
    name = "delivery_agent"
    step_id = "prepare_response_delivery"

    def run(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "intent_agent"))
        quality_check = QualityCheckResult.model_validate(_require_state_value(state, "quality_check", "quality_agent"))
        reply = ReplyDraft.model_validate(_require_state_value(state, "reply", "customer_service_agent"))
        tags = list(_require_state_value(state, "tags", "tagging_agent"))
        should_handoff = bool(_require_state_value(state, "should_handoff", "escalation_agent"))
        decision_reason = str(_require_state_value(state, "decision_reason", "escalation_agent"))
        tagging_service: TaggingService = state["services"]["tagging"]

        action: Literal["auto_replied", "pending_review"] = "auto_replied"
        final_reply = reply.draft_reply if quality_check.passed and not intent_result.needs_human else None
        reply_status = "sent" if final_reply else "pending_review"
        conversation_status = "open" if final_reply else "pending_review"
        risk_level = quality_check.risk_level
        follow_up_reason = None
        follow_up_priority = None

        if should_handoff:
            # 升级人工时仍给用户即时确认；原始草稿会留在 trace 元数据里，供客服复核。
            action = "pending_review"
            handoff_reply = human_handoff_template_for_intent(intent_result.intent)
            reply.draft_reply = handoff_reply
            reply.prompt_template = "human_handoff_template"
            reply.model_name = "system-template"
            reply.cited_knowledge_ids = []
            reply.risk_notes.append(f"multi_agent_decision: {decision_reason}")
            if "human_handoff_template_sent" not in reply.risk_notes:
                reply.risk_notes.append("human_handoff_template_sent")

            final_reply = handoff_reply
            reply_status = "sent"
            conversation_status = "pending_review"
            quality_check.review_mode = "manual_review"
            quality_check.suggestion = "Human handoff template sent and follow-up task should be created."
            follow_up_priority = tagging_service.priority(tags, quality_check)
            follow_up_reason = ";".join(tags) if tags else "needs_human_follow_up"

        payload = {
            "action": action,
            "final_reply": final_reply,
            "reply_status": reply_status,
            "conversation_status": conversation_status,
            "risk_level": risk_level,
            "follow_up_reason": follow_up_reason,
            "follow_up_priority": follow_up_priority,
            "reply": reply.model_dump(),
            "quality_check": quality_check.model_dump(),
        }
        output = f"action={action}, reply_status={reply_status}, conversation_status={conversation_status}"
        return {
            **payload,
            "agent_plan": _mark_plan_step(state, self.step_id, "completed"),
            "agent_trace": _append_agent_trace(
                state,
                step_id=self.step_id,
                output=output,
                arguments={},
                payload=payload,
            ),
        }


class LangGraphMultiAgentSystem:
    """Supervisor-driven role-agent graph for the customer-service workflow."""

    def __init__(self) -> None:
        self.supervisor = SupervisorAgent()
        self.graph = self._compile_graph()

    def _compile_graph(self):
        graph = StateGraph(CustomerSupportGraphState)
        graph.add_node(self.supervisor.name, self.supervisor.run)
        graph.add_node("intent_agent", IntentAgent().run)
        graph.add_node("retrieval_agent", RetrievalAgent().run)
        graph.add_node("customer_service_agent", CustomerServiceAgent().run)
        graph.add_node("quality_agent", QualityAgent().run)
        graph.add_node("tagging_agent", TaggingAgent().run)
        graph.add_node("escalation_agent", EscalationAgent().run)
        graph.add_node("delivery_agent", DeliveryAgent().run)
        graph.set_entry_point(self.supervisor.name)
        graph.add_conditional_edges(
            self.supervisor.name,
            self.supervisor.route,
            {
                "intent_agent": "intent_agent",
                "retrieval_agent": "retrieval_agent",
                "customer_service_agent": "customer_service_agent",
                "quality_agent": "quality_agent",
                "tagging_agent": "tagging_agent",
                "escalation_agent": "escalation_agent",
                "delivery_agent": "delivery_agent",
                "__end__": END,
            },
        )
        for _, agent, _, _ in ROLE_SEQUENCE:
            graph.add_edge(agent, self.supervisor.name)
        return graph.compile()

    async def arun(self, state: CustomerSupportGraphState) -> CustomerSupportGraphState:
        return await self.graph.ainvoke(state)


class MultiAgentRuntime:
    """Compatibility facade backed by a LangGraph supervisor multi-agent system."""

    def __init__(
        self,
        intent_service: IntentService,
        kb_service: KnowledgeBaseService,
        reply_service: ReplyService,
        quality_service: QualityService,
        tagging_service: TaggingService,
    ) -> None:
        self.intent_service = intent_service
        self.kb_service = kb_service
        self.reply_service = reply_service
        self.quality_service = quality_service
        self.tagging_service = tagging_service
        self.multi_agent_system = LangGraphMultiAgentSystem()

    async def run(
        self,
        request: ChannelEventRequest,
        conversation_id: str,
        history: list[str],
        runtime_config: dict,
    ) -> MultiAgentRunResult:
        initial_state: CustomerSupportGraphState = {
            "messages": [HumanMessage(content=request.content)],
            "request": request,
            "conversation_id": conversation_id,
            "history": history,
            "runtime_config": runtime_config,
            "services": {
                "intent": self.intent_service,
                "kb": self.kb_service,
                "reply": self.reply_service,
                "quality": self.quality_service,
                "tagging": self.tagging_service,
            },
            "agent_plan": [],
            "agent_trace": [],
        }
        state = await self.multi_agent_system.arun(initial_state)

        missing = [
            key
            for key in (
                "intent_result",
                "knowledge_hits",
                "reply",
                "quality_check",
                "tags",
                "should_handoff",
                "decision_reason",
                "action",
                "reply_status",
                "conversation_status",
                "risk_level",
            )
            if key not in state
        ]
        if missing:
            raise RuntimeError(f"LangGraph multi-agent system stopped before required state was complete: {', '.join(missing)}")

        return MultiAgentRunResult(
            intent_result=IntentResult.model_validate(state["intent_result"]),
            knowledge_hits=[KnowledgeHit.model_validate(item) for item in state["knowledge_hits"]],
            reply=ReplyDraft.model_validate(state["reply"]),
            quality_check=QualityCheckResult.model_validate(state["quality_check"]),
            tags=state.get("tags", []),
            action=state["action"],  # type: ignore[arg-type]
            final_reply=state.get("final_reply"),
            reply_status=state["reply_status"],
            conversation_status=state["conversation_status"],
            risk_level=state["risk_level"],
            follow_up_reason=state.get("follow_up_reason"),
            follow_up_priority=state.get("follow_up_priority"),
            agent_plan=state.get("agent_plan", []),
            agent_trace=state.get("agent_trace", []),
            should_handoff=bool(state["should_handoff"]),
            decision_reason=str(state["decision_reason"]),
        )
