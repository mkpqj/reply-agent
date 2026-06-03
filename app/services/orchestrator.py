from __future__ import annotations

from app.models.schemas import ChannelEventRequest, ProcessedEventResponse
from app.services.intent import IntentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.llm_gateway import require_llm_runtime
from app.services.multi_agent import MultiAgentRuntime
from app.services.quality import QualityService
from app.services.reply import ReplyService
from app.services.store import AgentStore
from app.services.tagging import TaggingService


class ConversationOrchestrator:
    def __init__(
        self,
        store: AgentStore,
        intent_service: IntentService,
        kb_service: KnowledgeBaseService,
        reply_service: ReplyService,
        quality_service: QualityService,
        tagging_service: TaggingService,
    ) -> None:
        self.store = store
        self.multi_agent_runtime = MultiAgentRuntime(
            intent_service=intent_service,
            kb_service=kb_service,
            reply_service=reply_service,
            quality_service=quality_service,
            tagging_service=tagging_service,
        )

    async def process_channel_event(self, request: ChannelEventRequest) -> ProcessedEventResponse:
        runtime_config = self.store.get_system_config()
        require_llm_runtime(runtime_config)
        conversation = self.store.ensure_conversation(request.conversation_id, request.shop_id, request.user_id)
        # 先落库用户消息，再调用 Agent；这样意图、知识命中、回复、
        # 质检和待跟进任务都能回溯到触发它们的那一轮对话。
        message_id = self.store.add_message(
            conversation_id=conversation["id"],
            sender_type="user",
            content=request.content,
            message_type=request.message_type,
            product_id=request.product_id,
            order_context=request.order_context.model_dump() if request.order_context else None,
            logistics_context=request.logistics_context.model_dump() if request.logistics_context else None,
        )

        agent_result = await self.multi_agent_runtime.run(
            request=request,
            conversation_id=conversation["id"],
            history=self.store.get_recent_history(conversation["id"]),
            runtime_config=runtime_config,
        )

        intent_result = agent_result.intent_result
        knowledge_hits = agent_result.knowledge_hits
        reply = agent_result.reply
        quality_check = agent_result.quality_check

        self.store.add_intent_result(message_id, intent_result.intent, intent_result.confidence, intent_result.signals)
        self.store.add_knowledge_hits(message_id, [item.model_dump() for item in knowledge_hits])
        self.store.replace_tags(conversation["id"], agent_result.tags)

        follow_up_task_id = None
        if agent_result.follow_up_reason and agent_result.follow_up_priority:
            # 只有在多 Agent 图完成最终投递决策后才创建待跟进任务，
            # 保证人工队列和最终回复状态保持一致。
            follow_up_task_id = self.store.create_follow_up_task(
                conversation["id"],
                message_id,
                agent_result.follow_up_reason,
                agent_result.follow_up_priority,
            )

        reply_id = self.store.add_reply_record(
            message_id=message_id,
            draft_reply=reply.draft_reply,
            final_reply=agent_result.final_reply,
            reply_status=agent_result.reply_status,
            prompt_template=reply.prompt_template,
            model_name=reply.model_name,
            cited_knowledge_ids=reply.cited_knowledge_ids,
            risk_notes=reply.risk_notes,
        )
        self.store.add_quality_check(
            reply_id=reply_id,
            passed=quality_check.passed,
            risk_level=quality_check.risk_level,
            issues=[issue.model_dump() for issue in quality_check.issues],
            suggestion=quality_check.suggestion,
            review_mode=quality_check.review_mode,
        )
        self.store.update_conversation(
            conversation_id=conversation["id"],
            current_intent=intent_result.intent,
            risk_level=agent_result.risk_level,
            status=agent_result.conversation_status,
        )

        return ProcessedEventResponse(
            conversation_id=conversation["id"],
            message_id=message_id,
            intent_result=intent_result,
            knowledge_hits=knowledge_hits,
            reply=reply,
            quality_check=quality_check,
            tags=agent_result.tags,
            action=agent_result.action,
            follow_up_task_id=follow_up_task_id,
            final_reply=agent_result.final_reply,
            agent_plan=agent_result.agent_plan,
            agent_trace=agent_result.agent_trace,
        )
