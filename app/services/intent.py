from app.core.config import HIGH_RISK_KEYWORDS
from app.models.schemas import IntentRecognizeRequest, IntentResult


INTENT_RULES = {
    "退换货": ["退货", "换货", "退掉", "换一个", "退款退货"],
    "售后": ["售后", "破损", "少件", "少发", "漏发", "补发", "退款", "赔付", "质量问题"],
    "催发货": ["发货", "什么时候发", "怎么还没发", "催发货", "物流", "到货", "几天到"],
    "价格咨询": ["便宜", "优惠", "券", "折扣", "最低价", "补差", "保价", "多少钱"],
    "售前咨询": [
        "材质",
        "尺寸",
        "颜色",
        "适合",
        "怎么选",
        "有没有货",
        "库存",
        "能不能",
        "是什么面料",
        "面料",
        "厚不厚",
        "多大",
        "多长",
        "多宽",
        "长度",
        "宽度",
        "大小",
    ],
}

GREETING_KEYWORDS = ["在吗", "有人吗", "你好", "您好", "哈喽", "hi", "hello"]


def is_greeting_message(message: str) -> bool:
    lowered = message.strip().lower()
    return any(word in lowered for word in GREETING_KEYWORDS)


class IntentService:
    def recognize(self, request: IntentRecognizeRequest) -> IntentResult:
        text = request.message.strip()
        lowered = text.lower()
        signals: list[str] = []
        intent = "其他"
        score = 0.45

        for label, keywords in INTENT_RULES.items():
            hits = [word for word in keywords if word in text or word in lowered]
            if hits:
                intent = label
                score = min(0.65 + len(hits) * 0.12, 0.97)
                signals.extend([f"命中关键词:{word}" for word in hits[:3]])
                break

        if request.order_context and request.order_context.status == "paid" and any(word in text for word in ["发货", "物流", "到货"]):
            intent = "催发货"
            score = max(score, 0.9)
            signals.append("订单已支付")

        if "退款" in text and any(word in text for word in ["破损", "少件", "少发", "漏发", "质量", "售后"]):
            intent = "售后"
            score = max(score, 0.9)
            signals.append("退款与售后关键词联合命中")

        if intent == "其他" and is_greeting_message(text):
            score = 0.88
            signals.append("命中寒暄问候")

        if any(word in text for word in HIGH_RISK_KEYWORDS):
            signals.append("命中高风险关键词")

        needs_human = score < 0.7
        if intent == "其他" and not signals:
            signals.append("未命中明确信图规则")

        return IntentResult(
            intent=intent,
            confidence=round(score, 2),
            signals=signals,
            needs_human=needs_human,
        )
