from __future__ import annotations

from app.core.database import init_db
from app.services.store import AgentStore


def main() -> None:
    store = AgentStore()
    init_db()
    store.reset_demo_data()

    conversation = store.ensure_conversation("conv_test_followup", "shop-demo", "user-demo")

    first_message_id = store.add_message(
        conversation_id=conversation["id"],
        sender_type="user",
        content="什么时候补货",
        message_type="text",
        product_id="sku-scarf",
        order_context=None,
        logistics_context=None,
    )
    first_task_id = store.create_follow_up_task(conversation["id"], first_message_id, "低置信度识别；知识未命中", "P2")

    second_message_id = store.add_message(
        conversation_id=conversation["id"],
        sender_type="user",
        content="少发了一条围巾",
        message_type="text",
        product_id="sku-scarf",
        order_context=None,
        logistics_context=None,
    )
    second_task_id = store.create_follow_up_task(conversation["id"], second_message_id, "低置信度识别；知识未命中", "P2")

    queue = store.list_follow_up_tasks()
    assert len(queue) == 2, f"expected 2 tasks, got {len(queue)}"
    queue_by_id = {item["id"]: item for item in queue}
    assert queue_by_id[first_task_id]["message_content"] == "什么时候补货"
    assert queue_by_id[second_task_id]["message_content"] == "少发了一条围巾"

    first_detail = store.get_follow_up_task_detail(first_task_id)
    second_detail = store.get_follow_up_task_detail(second_task_id)
    assert first_detail is not None
    assert second_detail is not None
    assert first_detail["source_message"]["content"] == "什么时候补货"
    assert second_detail["source_message"]["content"] == "少发了一条围巾"

    print("follow_up_queue_test_passed")


if __name__ == "__main__":
    main()
