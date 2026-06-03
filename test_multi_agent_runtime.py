from __future__ import annotations

import asyncio

from app.models.schemas import ChannelEventRequest, KnowledgeHit, ReplyDraft
from app.services.intent import IntentService
from app.services.multi_agent import AGENT_DEFINITIONS, MultiAgentRuntime
from app.services.quality import QualityService
from app.services.tagging import TaggingService


EXPECTED_ROLE_AGENTS = [
    "intent_agent",
    "retrieval_agent",
    "customer_service_agent",
    "quality_agent",
    "tagging_agent",
    "escalation_agent",
    "delivery_agent",
]


class FakeVectorIndex:
    class Gateway:
        def is_enabled(self) -> bool:
            return False

    embedding_gateway = Gateway()


class FakeKnowledgeBaseService:
    vector_index = FakeVectorIndex()
    last_vector_hit_count = 0

    async def search(self, request):
        return [
            KnowledgeHit(
                doc_id="kb_test",
                kb_type="faq",
                title="Shipping rule",
                content="Orders ship within 48 hours.",
                score=0.91,
            )
        ]


class FakeReplyService:
    async def generate(self, request, prompt_overrides=None, runtime_config=None):
        return ReplyDraft(
            draft_reply="Hello, orders ship within 48 hours. Please check the order page for updates.",
            prompt_template="test-template",
            cited_knowledge_ids=["kb_test"],
            risk_notes=[],
            model_name="fake-llm",
        )


async def run_scenario() -> None:
    assert [AGENT_DEFINITIONS[name].name for name in EXPECTED_ROLE_AGENTS] == EXPECTED_ROLE_AGENTS
    assert AGENT_DEFINITIONS["customer_service_agent"].uses_llm is True
    assert AGENT_DEFINITIONS["retrieval_agent"].uses_llm is False
    assert "KnowledgeBaseService.search" in AGENT_DEFINITIONS["retrieval_agent"].tools

    runtime = MultiAgentRuntime(
        intent_service=IntentService(),
        kb_service=FakeKnowledgeBaseService(),
        reply_service=FakeReplyService(),
        quality_service=QualityService(),
        tagging_service=TaggingService(),
    )
    result = await runtime.run(
        request=ChannelEventRequest(
            shop_id="shop-demo",
            user_id="user-demo",
            content="hello",
            product_id="sku-test",
        ),
        conversation_id="conv-test",
        history=[],
        runtime_config={
            "auto_reply_enabled": True,
            "intent_confidence_threshold": 0.7,
            "quality_block_on_sensitive_missing_kb": True,
        },
    )

    assert [step.agent for step in result.agent_plan] == EXPECTED_ROLE_AGENTS
    role_events = [event for event in result.agent_trace if event.action == "role_execute"]
    assert [event.agent for event in role_events] == EXPECTED_ROLE_AGENTS
    assert role_events[1].metadata["tools"] == ["KnowledgeBaseService.search", "VectorKnowledgeIndex.search"]
    assert role_events[2].metadata["model"] == "runtime.llm_model"
    assert result.agent_trace[0].agent == "supervisor_agent"
    print("multi_agent_runtime_test_passed")


def main() -> None:
    asyncio.run(run_scenario())


if __name__ == "__main__":
    main()
