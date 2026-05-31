from __future__ import annotations

from app.models.schemas import ChannelEventRequest, DemoScenarioRequest, OrderContext
from app.services.orchestrator import ConversationOrchestrator
from app.services.store import AgentStore


DEMO_SCENARIOS = {
    "售前咨询": ChannelEventRequest(
        shop_id="shop-demo",
        user_id="demo-user-presale",
        content="这条围巾是什么材质，适合冬天戴吗？",
        product_id="sku-scarf",
    ),
    "催发货": ChannelEventRequest(
        shop_id="shop-demo",
        user_id="demo-user-shipping",
        content="怎么还没发货，我想知道大概什么时候能发出",
        product_id="sku-scarf",
        order_context=OrderContext(status="paid", is_presale=False),
    ),
    "售后": ChannelEventRequest(
        shop_id="shop-demo",
        user_id="demo-user-aftersale",
        content="收到的商品有破损，想申请售后怎么处理？",
        product_id="sku-scarf",
        order_context=OrderContext(status="delivered", is_presale=False),
    ),
    "退换货": ChannelEventRequest(
        shop_id="shop-demo",
        user_id="demo-user-return",
        content="这个颜色不太适合我，签收后可以退换货吗？",
        product_id="sku-scarf",
        order_context=OrderContext(status="delivered", is_presale=False),
    ),
    "价格咨询": ChannelEventRequest(
        shop_id="shop-demo",
        user_id="demo-user-price",
        content="现在下单有优惠券吗，后面降价可以补差吗？",
        product_id="sku-scarf",
    ),
    "混合风险": ChannelEventRequest(
        shop_id="shop-demo",
        user_id="demo-user-risk",
        content="怎么还没发货，明天必须到，不然我就投诉平台，另外要给我补偿",
        product_id="sku-scarf",
        order_context=OrderContext(status="paid", is_presale=False),
    ),
}


class DemoService:
    def __init__(self, store: AgentStore, orchestrator: ConversationOrchestrator) -> None:
        self.store = store
        self.orchestrator = orchestrator

    async def seed_demo_data(self) -> dict[str, int]:
        self.store.reset_demo_data()
        for scenario in DEMO_SCENARIOS.values():
            await self.orchestrator.process_channel_event(scenario)
        return {
            "seeded_conversations": len(DEMO_SCENARIOS),
            "seeded_scenarios": len(DEMO_SCENARIOS),
        }

    async def run_scenario(self, request: DemoScenarioRequest):
        scenario = DEMO_SCENARIOS[request.scenario]
        return await self.orchestrator.process_channel_event(scenario)
