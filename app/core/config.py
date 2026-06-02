from pathlib import Path
import os
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "agent.db"
KB_PATH = DATA_DIR / "knowledge_base.json"
KB_VECTOR_INDEX_PATH = DATA_DIR / "kb_vector_index.json"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

INTENT_PROMPTS = {
    "售前咨询": {
        "template_name": "presale_v1",
        "instructions": "只回答商品信息、使用建议和页面已展示活动，不承诺库存、赠品或到货时间。",
    },
    "催发货": {
        "template_name": "shipping_v1",
        "instructions": "必须基于订单状态和物流规则回答，不承诺具体发货或到达时间。",
    },
    "售后": {
        "template_name": "aftersale_v1",
        "instructions": "严格引用售后政策，不直接允诺退款、赔付或补发。",
    },
    "退换货": {
        "template_name": "exchange_v1",
        "instructions": "明确退换条件、流程和入口，不越权批准特殊退换。",
    },
    "价格咨询": {
        "template_name": "price_v1",
        "instructions": "只说明当前活动和可见规则，不承诺补差或永久保价。",
    },
    "其他": {
        "template_name": "fallback_v1",
        "instructions": "无法确认问题时给出保守回复，并建议人工协助。",
    },
}

PROMISE_RISK_PATTERNS = [
    "一定今天发",
    "保证今天发",
    "明天必到",
    "百分百到",
    "马上退款给您",
    "直接补偿",
    "无条件退",
    "立刻处理完成",
]

SENSITIVE_INTENTS = {"催发货", "售后", "退换货", "价格咨询"}
HIGH_RISK_KEYWORDS = ["投诉", "差评", "举报", "平台介入", "赔偿", "赔付", "退款", "补发"]
EMOTION_KEYWORDS = ["生气", "太慢了", "差评", "投诉", "失望", "离谱"]
