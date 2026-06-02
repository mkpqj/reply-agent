from __future__ import annotations

from app.models.schemas import (
    ChannelEventRequest,
    IntentRecognizeRequest,
    KbSearchRequest,
    ProcessedEventResponse,
    ReplyCheckRequest,
    ReplyGenerateRequest,
)
from app.services.intent import IntentService
from app.services.intent import is_greeting_message
from app.services.knowledge_base import KnowledgeBaseService
from app.services.quality import QualityService
from app.services.reply import ReplyService
from app.services.store import AgentStore
from app.services.tagging import TaggingService


HUMAN_HANDOFF_TEMPLATES = {
    "售前咨询": "您好，已收到您关于商品信息的咨询。由于当前需要进一步核实细节，我暂时无法给您准确结论。建议您直接联系人工客服，他们会结合商品实际情况尽快为您确认。感谢您的理解！",
    "催发货": "您好，非常理解您希望尽快了解发货进度。由于当前信息有限，我暂时无法确认具体的发货或到货安排。建议您直接联系人工客服，他们会帮您核实并尽快处理。感谢您的理解！",
    "售后": "您好，非常抱歉给您带来困扰。您反馈的售后问题我已经收到，但由于当前还需要进一步核实订单和处理信息，我暂时无法直接确认最终方案。建议您直接联系人工客服，他们会尽快帮您核实并处理。感谢您的理解！",
    "退换货": "您好，关于您提到的退换货问题，我已经收到反馈。由于当前还需要结合订单和售后规则进一步确认，我暂时无法直接给您最终处理结论。建议您直接联系人工客服，他们会尽快帮您核实并处理。感谢您的理解！",
    "价格咨询": "您好，关于您咨询的价格或优惠问题，我已经收到。由于当前还需要进一步核实活动和订单信息，我暂时无法直接为您确认最终价格方案。建议您直接联系人工客服，他们会尽快帮您核实并处理。感谢您的理解！",
    "其他": "您好，您反馈的问题我已经收到。由于当前信息有限，我暂时无法给您准确结论。建议您直接联系人工客服，他们会帮您进一步核实并尽快处理。感谢您的理解！",
}


def human_handoff_template_for_intent(intent: str) -> str:
    return HUMAN_HANDOFF_TEMPLATES.get(intent, HUMAN_HANDOFF_TEMPLATES["其他"])


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
        self.intent_service = intent_service
        self.kb_service = kb_service
        self.reply_service = reply_service
        self.quality_service = quality_service
        self.tagging_service = tagging_service

    async def process_channel_event(self, request: ChannelEventRequest) -> ProcessedEventResponse:
        runtime_config = self.store.get_system_config()
        conversation = self.store.ensure_conversation(request.conversation_id, request.shop_id, request.user_id)
        message_id = self.store.add_message(
            conversation_id=conversation["id"],
            sender_type="user",
            content=request.content,
            message_type=request.message_type,
            product_id=request.product_id,
            order_context=request.order_context.model_dump() if request.order_context else None,
            logistics_context=request.logistics_context.model_dump() if request.logistics_context else None,
        )

        history = self.store.get_recent_history(conversation["id"])
        intent_result = self.intent_service.recognize(
            IntentRecognizeRequest(
                conversation_id=conversation["id"],
                message=request.content,
                order_context=request.order_context,
            )
        )
        intent_result.needs_human = intent_result.confidence < runtime_config.get("intent_confidence_threshold", 0.7)
        self.store.add_intent_result(message_id, intent_result.intent, intent_result.confidence, intent_result.signals)

        knowledge_hits = self.kb_service.search(
            KbSearchRequest(
                shop_id=request.shop_id,
                intent=intent_result.intent,
                query=request.content,
                product_id=request.product_id,
            )
        )
        self.store.add_knowledge_hits(message_id, [item.model_dump() for item in knowledge_hits])

        reply = await self.reply_service.generate(
            ReplyGenerateRequest(
                intent=intent_result.intent,
                user_message=request.content,
                shop_id=request.shop_id,
                product_id=request.product_id,
                conversation_history=history,
                order_context=request.order_context,
                logistics_context=request.logistics_context,
                knowledge_hits=[item.model_dump() for item in knowledge_hits],
            ),
            prompt_overrides=runtime_config.get("prompts"),
            runtime_config=runtime_config,
        )

        quality_check = self.quality_service.check(
            ReplyCheckRequest(
                intent=intent_result.intent,
                user_message=request.content,
                draft_reply=reply.draft_reply,
                knowledge_hits=[item.model_dump() for item in knowledge_hits],
            ),
            config=runtime_config,
        )

        tags = self.tagging_service.generate_tags(
            message=request.content,
            intent_result=intent_result,
            quality_result=quality_check,
            knowledge_hit_count=len(knowledge_hits),
        )
        if intent_result.intent == "其他" and is_greeting_message(request.content):
            tags = [tag for tag in tags if tag not in {"低置信度识别", "知识未命中"}]
        self.store.replace_tags(conversation["id"], tags)

        needs_follow_up = (
            self.tagging_service.needs_follow_up(tags, quality_check)
            or intent_result.needs_human
            or not runtime_config.get("auto_reply_enabled", True)
        )
        if intent_result.intent == "其他" and is_greeting_message(request.content):
            needs_follow_up = False

        follow_up_task_id = None
        action = "auto_replied"
        final_reply = reply.draft_reply if quality_check.passed and not intent_result.needs_human else None
        reply_status = "sent" if final_reply else "pending_review"
        conversation_status = "open" if final_reply else "pending_review"
        risk_level = quality_check.risk_level

        if needs_follow_up:
            action = "pending_review"
            handoff_reply = human_handoff_template_for_intent(intent_result.intent)
            reply.draft_reply = handoff_reply
            reply.prompt_template = "human_handoff_template"
            reply.model_name = "system-template"
            reply.cited_knowledge_ids = []
            if "已发送人工客服引导模板回复。" not in reply.risk_notes:
                reply.risk_notes.append("已发送人工客服引导模板回复。")

            final_reply = handoff_reply
            reply_status = "sent"
            conversation_status = "pending_review"
            quality_check.review_mode = "manual_review"
            quality_check.suggestion = "已发送人工客服引导模板，并加入待处理任务队列。"
            priority = self.tagging_service.priority(tags, quality_check)
            reason = "；".join(tags) if tags else "需要人工跟进"
            follow_up_task_id = self.store.create_follow_up_task(conversation["id"], message_id, reason, priority)

        reply_id = self.store.add_reply_record(
            message_id=message_id,
            draft_reply=reply.draft_reply,
            final_reply=final_reply,
            reply_status=reply_status,
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
            risk_level=risk_level,
            status=conversation_status,
        )

        return ProcessedEventResponse(
            conversation_id=conversation["id"],
            message_id=message_id,
            intent_result=intent_result,
            knowledge_hits=knowledge_hits,
            reply=reply,
            quality_check=quality_check,
            tags=tags,
            action=action,
            follow_up_task_id=follow_up_task_id,
            final_reply=final_reply,
        )
