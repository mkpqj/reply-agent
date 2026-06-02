from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode, tools_condition
from langgraph.types import Command

from app.core.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
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


class CustomerSupportGraphState(TypedDict, total=False):
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
    llm_tool_planner_failed: bool
    agent_plan: list[AgentPlanStep]
    agent_trace: list[AgentTraceEvent]
    retrieval_metadata: dict[str, Any]


TOOL_ORDER = [
    "recognize_intent",
    "search_knowledge",
    "draft_reply",
    "check_reply",
    "tag_conversation",
    "route_conversation",
    "prepare_response_delivery",
]


def _tool_call_id(state: CustomerSupportGraphState) -> str:
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return str(last_message.tool_calls[0]["id"])
    return f"call_{uuid.uuid4().hex[:12]}"


def _tool_message(state: CustomerSupportGraphState, name: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id=_tool_call_id(state))


def _json_content(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _require_state_value(state: CustomerSupportGraphState, key: str, dependency: str) -> Any:
    value = state.get(key)
    if value is None:
        raise RuntimeError(f"Agent tool requires `{dependency}` to run first.")
    return value


def _history_query(message: str, history: list[str]) -> str:
    useful_history = [item for item in history[-3:] if item and item != message]
    if not useful_history:
        return message
    return " ".join([*useful_history, message])


def _merge_hits(primary: list[KnowledgeHit], secondary: list[KnowledgeHit]) -> list[KnowledgeHit]:
    merged: dict[str, KnowledgeHit] = {item.doc_id: item for item in primary}
    for item in secondary:
        existing = merged.get(item.doc_id)
        if not existing or item.score > existing.score:
            merged[item.doc_id] = item
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:5]


def _result_metadata_for_tool(tool_name: str, state: CustomerSupportGraphState, payload: Any) -> Any:
    if tool_name == "search_knowledge":
        return {
            "hits": payload,
            "retrieval": state.get("retrieval_metadata", {}),
        }
    return payload


def _append_tool_trace(
    state: CustomerSupportGraphState,
    *,
    tool_name: str,
    output: str,
    arguments: dict[str, Any],
    payload: Any,
) -> list[AgentTraceEvent]:
    trace = list(state.get("agent_trace", []))
    trace.append(
        AgentTraceEvent(
            step_id=tool_name,
            agent="langgraph_tool_agent",
            action="tool_call",
            output=output,
            metadata={
                "tool_call_id": _tool_call_id(state),
                "tool_name": tool_name,
                "arguments": arguments,
                "result": _result_metadata_for_tool(tool_name, state, payload),
            },
        )
    )
    return trace


def _mark_plan_step(state: CustomerSupportGraphState, step_id: str, status: Literal["completed", "running"]) -> list[AgentPlanStep]:
    plan = list(state.get("agent_plan", []))
    for step in plan:
        if step.step_id == step_id:
            step.status = status
            break
    return plan


@tool
def recognize_intent(confidence_threshold: float, state: Annotated[CustomerSupportGraphState, InjectedState]) -> Command:
    """Infer the customer intent and whether confidence requires human review."""
    request = state["request"]
    intent_service: IntentService = state["services"]["intent"]
    result = intent_service.recognize(
        IntentRecognizeRequest(
            conversation_id=state["conversation_id"],
            message=request.content,
            order_context=request.order_context,
        )
    )
    result.needs_human = result.confidence < confidence_threshold
    payload = result.model_dump()
    output = f"recognize_intent: intent={result.intent}, confidence={result.confidence}, needs_human={result.needs_human}"
    return Command(
        update={
            "intent_result": payload,
            "agent_plan": _mark_plan_step(state, "recognize_intent", "completed"),
            "agent_trace": _append_tool_trace(
                state,
                tool_name="recognize_intent",
                output=output,
                arguments={"confidence_threshold": confidence_threshold},
                payload=payload,
            ),
            "messages": [_tool_message(state, "recognize_intent", _json_content(payload))],
        }
    )


@tool
async def search_knowledge(
    intent: str,
    query: str,
    product_id: str | None,
    state: Annotated[CustomerSupportGraphState, InjectedState],
) -> Command:
    """Search the merchant knowledge base with RAG-style retrieval."""
    request = state["request"]
    kb_service: KnowledgeBaseService = state["services"]["kb"]
    hits = await kb_service.search(
        KbSearchRequest(
            shop_id=request.shop_id,
            intent=intent,
            query=query,
            product_id=product_id,
        )
    )

    expanded_query = _history_query(query, state.get("history", []))
    if expanded_query and expanded_query != query:
        extra_hits = await kb_service.search(
            KbSearchRequest(
                shop_id=request.shop_id,
                intent=intent,
                query=expanded_query,
                product_id=product_id,
            )
        )
        hits = _merge_hits(hits, extra_hits)

    payload = [item.model_dump() for item in hits]
    retrieval_metadata = {
        "query": query,
        "expanded_query": expanded_query or query,
        "doc_ids": [item.doc_id for item in hits],
        "scores": [item.score for item in hits],
        "embedding_enabled": kb_service.vector_index.embedding_gateway.is_enabled(),
        "vector_hits": kb_service.last_vector_hit_count,
    }
    state_with_retrieval = {**state, "retrieval_metadata": retrieval_metadata}
    output = f"search_knowledge: returned {len(hits)} items"
    return Command(
        update={
            "knowledge_hits": payload,
            "retrieval_metadata": retrieval_metadata,
            "agent_plan": _mark_plan_step(state, "search_knowledge", "completed"),
            "agent_trace": _append_tool_trace(
                state_with_retrieval,
                tool_name="search_knowledge",
                output=output,
                arguments={"intent": intent, "query": query, "product_id": product_id},
                payload=payload,
            ),
            "messages": [_tool_message(state, "search_knowledge", _json_content({"hits": payload, "retrieval": retrieval_metadata}))],
        }
    )


@tool
async def draft_reply(intent: str, state: Annotated[CustomerSupportGraphState, InjectedState]) -> Command:
    """Draft a grounded customer-facing reply from retrieved evidence."""
    request = state["request"]
    reply_service: ReplyService = state["services"]["reply"]
    knowledge_hits = _require_state_value(state, "knowledge_hits", "search_knowledge")
    reply = await reply_service.generate(
        ReplyGenerateRequest(
            intent=intent,
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
    output = f"draft_reply: drafted reply with {len(reply.cited_knowledge_ids)} citations via {reply.model_name}"
    return Command(
        update={
            "reply": payload,
            "agent_plan": _mark_plan_step(state, "draft_reply", "completed"),
            "agent_trace": _append_tool_trace(
                state,
                tool_name="draft_reply",
                output=output,
                arguments={"intent": intent},
                payload=payload,
            ),
            "messages": [_tool_message(state, "draft_reply", _json_content(payload))],
        }
    )


@tool
def check_reply(intent: str, state: Annotated[CustomerSupportGraphState, InjectedState]) -> Command:
    """Check the draft reply for policy, quality, and promise risk."""
    request = state["request"]
    quality_service: QualityService = state["services"]["quality"]
    reply = ReplyDraft.model_validate(_require_state_value(state, "reply", "draft_reply"))
    knowledge_hits = _require_state_value(state, "knowledge_hits", "search_knowledge")
    quality_check = quality_service.check(
        ReplyCheckRequest(
            intent=intent,
            user_message=request.content,
            draft_reply=reply.draft_reply,
            knowledge_hits=knowledge_hits,
        ),
        config=state.get("runtime_config", {}),
    )
    payload = quality_check.model_dump()
    output = f"check_reply: passed={quality_check.passed}, risk={quality_check.risk_level}, review_mode={quality_check.review_mode}"
    return Command(
        update={
            "quality_check": payload,
            "agent_plan": _mark_plan_step(state, "check_reply", "completed"),
            "agent_trace": _append_tool_trace(
                state,
                tool_name="check_reply",
                output=output,
                arguments={"intent": intent},
                payload=payload,
            ),
            "messages": [_tool_message(state, "check_reply", _json_content(payload))],
        }
    )


@tool
def tag_conversation(state: Annotated[CustomerSupportGraphState, InjectedState]) -> Command:
    """Generate operational tags for follow-up, risk, and dashboard routing."""
    request = state["request"]
    tagging_service: TaggingService = state["services"]["tagging"]
    intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "recognize_intent"))
    quality_check = QualityCheckResult.model_validate(_require_state_value(state, "quality_check", "check_reply"))
    knowledge_hits = _require_state_value(state, "knowledge_hits", "search_knowledge")

    tags = tagging_service.generate_tags(
        message=request.content,
        intent_result=intent_result,
        quality_result=quality_check,
        knowledge_hit_count=len(knowledge_hits),
    )
    if intent_result.intent == "其他" and is_greeting_message(request.content):
        tags = [tag for tag in tags if tag not in {"低置信度识别", "知识未命中"}]

    payload = {"tags": tags}
    output = f"tag_conversation: generated {len(tags)} tags"
    return Command(
        update={
            "tags": tags,
            "agent_plan": _mark_plan_step(state, "tag_conversation", "completed"),
            "agent_trace": _append_tool_trace(
                state,
                tool_name="tag_conversation",
                output=output,
                arguments={},
                payload=payload,
            ),
            "messages": [_tool_message(state, "tag_conversation", _json_content(payload))],
        }
    )


@tool
def route_conversation(state: Annotated[CustomerSupportGraphState, InjectedState]) -> Command:
    """Choose auto-reply or human follow-up based on accumulated tool results."""
    intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "recognize_intent"))
    quality_check = QualityCheckResult.model_validate(_require_state_value(state, "quality_check", "check_reply"))
    tagging_service: TaggingService = state["services"]["tagging"]
    tags = list(_require_state_value(state, "tags", "tag_conversation"))
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
        reasons = [reason for reason in reasons if reason != "tagging_requires_follow_up"]

    should_handoff = bool(reasons)
    decision_reason = ", ".join(reasons) if reasons else "all_tools_green"
    payload = {
        "decision": "human_follow_up" if should_handoff else "auto_reply",
        "reason": decision_reason,
        "reasons": reasons,
    }
    output = f"route_conversation: decision={payload['decision']}; reason={decision_reason}"
    return Command(
        update={
            "should_handoff": should_handoff,
            "decision_reason": decision_reason,
            "agent_plan": _mark_plan_step(state, "route_conversation", "completed"),
            "agent_trace": _append_tool_trace(
                state,
                tool_name="route_conversation",
                output=output,
                arguments={},
                payload=payload,
            ),
            "messages": [_tool_message(state, "route_conversation", _json_content(payload))],
        }
    )


@tool
def prepare_response_delivery(state: Annotated[CustomerSupportGraphState, InjectedState]) -> Command:
    """Prepare the final send/review payload and human follow-up task metadata."""
    intent_result = IntentResult.model_validate(_require_state_value(state, "intent_result", "recognize_intent"))
    quality_check = QualityCheckResult.model_validate(_require_state_value(state, "quality_check", "check_reply"))
    reply = ReplyDraft.model_validate(_require_state_value(state, "reply", "draft_reply"))
    tags = list(_require_state_value(state, "tags", "tag_conversation"))
    should_handoff = bool(_require_state_value(state, "should_handoff", "route_conversation"))
    decision_reason = str(_require_state_value(state, "decision_reason", "route_conversation"))
    tagging_service: TaggingService = state["services"]["tagging"]

    action: Literal["auto_replied", "pending_review"] = "auto_replied"
    final_reply = reply.draft_reply if quality_check.passed and not intent_result.needs_human else None
    reply_status = "sent" if final_reply else "pending_review"
    conversation_status = "open" if final_reply else "pending_review"
    risk_level = quality_check.risk_level
    follow_up_reason = None
    follow_up_priority = None

    if should_handoff:
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
    output = f"prepare_response_delivery: action={action}, reply_status={reply_status}, conversation_status={conversation_status}"
    return Command(
        update={
            **payload,
            "agent_plan": _mark_plan_step(state, "prepare_response_delivery", "completed"),
            "agent_trace": _append_tool_trace(
                state,
                tool_name="prepare_response_delivery",
                output=output,
                arguments={},
                payload=payload,
            ),
            "messages": [_tool_message(state, "prepare_response_delivery", _json_content(payload))],
        }
    )


class LangGraphToolAgent:
    name = "langgraph_tool_agent"

    def __init__(self, tools: list[BaseTool]) -> None:
        self.tools = tools
        self.tool_names = [item.name for item in tools]
        self.graph = self._compile_graph()

    def _compile_graph(self):
        graph = StateGraph(CustomerSupportGraphState)
        graph.add_node("agent", self._call_model)
        graph.add_node("tools", ToolNode(self.tools))
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": END})
        graph.add_edge("tools", "agent")
        return graph.compile()

    async def arun(self, state: CustomerSupportGraphState) -> CustomerSupportGraphState:
        return await self.graph.ainvoke(state)

    async def _call_model(self, state: CustomerSupportGraphState, config: RunnableConfig) -> dict[str, Any]:
        if not state.get("agent_plan"):
            return self._plan_first_tool(state)

        tool_call = self._next_deterministic_tool_call(state)
        if tool_call is None:
            return {"messages": [AIMessage(content="tool agent completed")]}

        if self._llm_enabled(state):
            try:
                message = await self._llm_tool_call(state, config)
            except Exception as exc:
                trace = list(state.get("agent_trace", []))
                trace.append(
                    AgentTraceEvent(
                        step_id="llm_tool_planner",
                        agent=self.name,
                        action="fallback_to_deterministic_planner",
                        output=f"LLM tool planner failed: {exc.__class__.__name__}",
                        metadata={"error": str(exc)},
                    )
                )
                return {
                    "llm_tool_planner_failed": True,
                    "agent_trace": trace,
                    "messages": [AIMessage(content="", tool_calls=[tool_call])],
                }
            else:
                if isinstance(message, AIMessage) and message.tool_calls:
                    return {"messages": [message]}

        return {"messages": [AIMessage(content="", tool_calls=[tool_call])]}

    def _plan_first_tool(self, state: CustomerSupportGraphState) -> dict[str, Any]:
        observations: list[str] = []
        request = state["request"]
        if state.get("history"):
            observations.append(f"Use {min(len(state['history']), 6)} recent turns as conversation memory.")
        if request.order_context:
            observations.append("Order context is available for policy-sensitive tools.")
        if request.logistics_context:
            observations.append("Logistics context is available for shipping-sensitive tools.")
        if request.product_id:
            observations.append(f"Prefer product-scoped retrieval for product_id={request.product_id}.")

        plan = [
            AgentPlanStep(
                step_id=tool.name,
                agent=self.name,
                objective=f"Call tool `{tool.name}`. {tool.description or ''}".strip(),
                observations=observations if index == 0 else [],
            )
            for index, tool in enumerate(self.tools)
        ]
        trace = list(state.get("agent_trace", []))
        trace.append(
            AgentTraceEvent(
                step_id="plan_tool_calls",
                agent=self.name,
                action="plan_tool_calls",
                output=f"planned {len(plan)} LangGraph tool calls",
                metadata={
                    "framework": "langgraph",
                    "available_tools": self.tool_names,
                    "planned_tools": TOOL_ORDER,
                    "llm_tool_planner_enabled": self._llm_enabled(state),
                },
            )
        )
        first_call = self._build_tool_call(
            "recognize_intent",
            {"confidence_threshold": state.get("runtime_config", {}).get("intent_confidence_threshold", 0.7)},
        )
        return {
            "agent_plan": plan,
            "agent_trace": trace,
            "messages": [AIMessage(content="", tool_calls=[first_call])],
        }

    def _next_deterministic_tool_call(self, state: CustomerSupportGraphState) -> dict[str, Any] | None:
        request = state["request"]
        runtime_config = state.get("runtime_config", {})
        if not state.get("intent_result"):
            return self._build_tool_call(
                "recognize_intent",
                {"confidence_threshold": runtime_config.get("intent_confidence_threshold", 0.7)},
            )
        intent_result = IntentResult.model_validate(state["intent_result"])
        if state.get("knowledge_hits") is None:
            return self._build_tool_call(
                "search_knowledge",
                {
                    "intent": intent_result.intent,
                    "query": request.content,
                    "product_id": request.product_id,
                },
            )
        if not state.get("reply"):
            return self._build_tool_call("draft_reply", {"intent": intent_result.intent})
        if not state.get("quality_check"):
            return self._build_tool_call("check_reply", {"intent": intent_result.intent})
        if state.get("tags") is None:
            return self._build_tool_call("tag_conversation", {})
        if state.get("should_handoff") is None:
            return self._build_tool_call("route_conversation", {})
        if state.get("action") is None:
            return self._build_tool_call("prepare_response_delivery", {})
        return None

    async def _llm_tool_call(self, state: CustomerSupportGraphState, config: RunnableConfig) -> AIMessage:
        model = ChatOpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            model=state.get("runtime_config", {}).get("llm_model", LLM_MODEL),
            temperature=0,
        ).bind_tools(self.tools)
        messages = [SystemMessage(content=self._system_prompt()), *state["messages"]]
        response = await model.ainvoke(messages, config)
        return response

    def _llm_enabled(self, state: CustomerSupportGraphState) -> bool:
        return (
            bool(state.get("runtime_config", {}).get("llm_enabled"))
            and bool(LLM_API_KEY)
            and not bool(state.get("llm_tool_planner_failed"))
        )

    def _build_tool_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "args": args,
            "id": f"{name}_{uuid.uuid4().hex[:12]}",
            "type": "tool_call",
        }

    def _system_prompt(self) -> str:
        return (
            "You are a Xiaohongshu merchant customer-service tool-calling agent. "
            "Use the available tools to recognize intent, retrieve knowledge, draft a grounded reply, "
            "check quality, and route the conversation. Do not skip required tools."
        )


class MultiAgentRuntime:
    """Compatibility facade backed by a real LangGraph tool-calling agent."""

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
        self.tool_agent = LangGraphToolAgent(
            tools=[
                recognize_intent,
                search_knowledge,
                draft_reply,
                check_reply,
                tag_conversation,
                route_conversation,
                prepare_response_delivery,
            ]
        )

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
        state = await self.tool_agent.arun(initial_state)

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
            raise RuntimeError(f"LangGraph tool agent stopped before required state was complete: {', '.join(missing)}")

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
