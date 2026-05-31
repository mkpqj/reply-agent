from __future__ import annotations

from typing import Any

from app.core.config import INTENT_PROMPTS, SENSITIVE_INTENTS
from app.models.schemas import KnowledgeHit, ReplyDraft, ReplyGenerateRequest
from app.services.llm_gateway import LlmGateway


class ReplyService:
    def __init__(self, llm_gateway: LlmGateway | None = None) -> None:
        self.llm_gateway = llm_gateway or LlmGateway()

    async def generate(
        self,
        request: ReplyGenerateRequest,
        prompt_overrides: dict[str, Any] | None = None,
        runtime_config: dict[str, Any] | None = None,
    ) -> ReplyDraft:
        prompts = prompt_overrides or INTENT_PROMPTS
        prompt_config = prompts.get(request.intent, prompts["其他"])
        knowledge_hits = [KnowledgeHit.model_validate(hit) if isinstance(hit, dict) else hit for hit in request.knowledge_hits]
        cited_ids = [item.doc_id for item in knowledge_hits]
        risk_notes: list[str] = []

        if request.intent in SENSITIVE_INTENTS and not knowledge_hits:
            draft = "这类问题需要结合订单和店铺规则进一步确认，我先帮您记录下来，并建议由人工客服尽快为您核实处理。"
            risk_notes.append("敏感场景未命中知识库，已切换保守回复。")
            return ReplyDraft(
                draft_reply=draft,
                prompt_template=prompt_config["template_name"],
                cited_knowledge_ids=cited_ids,
                risk_notes=risk_notes,
            )

        llm_reply = await self.llm_gateway.generate_reply(
            intent=request.intent,
            user_message=request.user_message,
            instructions=prompt_config["instructions"],
            knowledge_context=[item.model_dump() for item in knowledge_hits],
            conversation_history=request.conversation_history,
            runtime_config=runtime_config,
        )
        if llm_reply:
            risk_notes.append("使用真实大模型生成回复。")
            risk_notes.append(prompt_config["instructions"])
            return ReplyDraft(
                draft_reply=llm_reply,
                prompt_template=prompt_config["template_name"],
                cited_knowledge_ids=cited_ids,
                risk_notes=risk_notes,
                model_name=(runtime_config or {}).get("llm_model", "llm-runtime"),
            )

        lines = []
        if request.intent == "售前咨询":
            knowledge_text = knowledge_hits[0].content if knowledge_hits else "目前商品信息以详情页展示为准。"
            lines.append(f"您好，这边帮您确认了，{knowledge_text}")
            lines.append("如果您方便的话，也可以告诉我更关注材质、尺寸还是搭配场景，我再继续帮您判断。")
        elif request.intent == "催发货":
            order_status = request.order_context.status if request.order_context else "未知"
            lines.append(f"理解您着急收货的心情，这边先帮您看了一下，当前订单状态为 {order_status}。")
            if knowledge_hits:
                lines.append(knowledge_hits[0].content)
            lines.append("具体发出和到达时间还需要以订单页物流更新为准，我会建议您留意订单最新状态。")
        elif request.intent == "售后":
            policy = knowledge_hits[0].content if knowledge_hits else "建议您通过订单售后入口发起申请。"
            lines.append(f"这边先和您说明一下处理规则，{policy}")
            lines.append("为了避免信息偏差，涉及退款、赔付或补发的结果还需要以平台审核和订单实际情况为准。")
        elif request.intent == "退换货":
            policy = knowledge_hits[0].content if knowledge_hits else "建议您在订单详情页进入售后入口提交申请。"
            lines.append(f"关于退换货，{policy}")
            lines.append("您可以先从订单售后入口提交申请，平台审核通过后会进入后续流程。")
        elif request.intent == "价格咨询":
            policy = knowledge_hits[0].content if knowledge_hits else "优惠和价格请以下单页实时展示为准。"
            lines.append(f"关于价格这边帮您确认到，{policy}")
            lines.append("如果您准备下单，可以先看一下商品页和结算页是否有可领取优惠。")
        else:
            lines.append("这边已经收到您的问题了。")
            lines.append("为了给您准确答复，我建议转由人工客服进一步核实后回复您。")
            risk_notes.append("意图未明确，已使用兜底回复。")

        history_tail = request.conversation_history[-1] if request.conversation_history else ""
        if history_tail and history_tail != request.user_message:
            risk_notes.append("已参考最近一轮会话上下文。")

        risk_notes.append(prompt_config["instructions"])
        return ReplyDraft(
            draft_reply="".join(lines),
            prompt_template=prompt_config["template_name"],
            cited_knowledge_ids=cited_ids,
            risk_notes=risk_notes,
        )
