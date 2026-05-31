from __future__ import annotations

from typing import Any

import httpx

from app.core.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


class LlmGateway:
    def is_enabled(self, runtime_config: dict[str, Any] | None = None) -> bool:
        config_enabled = bool((runtime_config or {}).get("llm_enabled", False))
        return config_enabled and bool(LLM_API_KEY)

    async def generate_reply(
        self,
        *,
        intent: str,
        user_message: str,
        instructions: str,
        knowledge_context: list[dict[str, Any]],
        conversation_history: list[str],
        runtime_config: dict[str, Any] | None = None,
    ) -> str | None:
        if not self.is_enabled(runtime_config):
            return None

        model = (runtime_config or {}).get("llm_model", LLM_MODEL)
        knowledge_text = "\n".join(
            [f"- [{item['kb_type']}] {item['title']}: {item['content']}" for item in knowledge_context]
        ) or "无命中知识，请给出保守回复。"
        history_text = "\n".join([f"- {line}" for line in conversation_history[-6:]]) or "无历史会话"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是小红书商家客服Agent，请严格基于知识库和业务约束回复。"
                        "不要承诺退款、赔付、时效，除非知识明确支持。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"意图: {intent}\n"
                        f"回复约束: {instructions}\n"
                        f"历史会话:\n{history_text}\n"
                        f"知识库命中:\n{knowledge_text}\n"
                        f"用户最新消息: {user_message}\n"
                        "请输出一段适合直接发送给用户的简洁中文回复。"
                    ),
                },
            ],
            "temperature": 0.3,
        }

        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        return data["choices"][0]["message"]["content"].strip()
