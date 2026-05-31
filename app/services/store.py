from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.database import get_connection, init_db
from app.core.config import INTENT_PROMPTS, PROMISE_RISK_PATTERNS


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AgentStore:
    def _default_config(self) -> dict[str, Any]:
        return {
            "auto_reply_enabled": True,
            "intent_confidence_threshold": 0.7,
            "quality_block_on_sensitive_missing_kb": True,
            "prompts": INTENT_PROMPTS,
            "promise_risk_patterns": PROMISE_RISK_PATTERNS,
            "llm_enabled": False,
            "llm_model": "gpt-4.1-mini",
        }

    def ensure_default_config(self) -> None:
        init_db()
        current_time = now_iso()
        default_config = self._default_config()
        with get_connection() as conn:
            row = conn.execute("SELECT value_json FROM system_configs WHERE key = ?", ("agent_settings",)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO system_configs (key, value_json, updated_at) VALUES (?, ?, ?)",
                    ("agent_settings", json.dumps(default_config, ensure_ascii=False), current_time),
                )
                return

            existing = json.loads(row["value_json"])
            merged = {**default_config, **existing}
            if merged != existing:
                conn.execute(
                    "UPDATE system_configs SET value_json = ?, updated_at = ? WHERE key = ?",
                    (json.dumps(merged, ensure_ascii=False), current_time, "agent_settings"),
                )

    def reset_demo_data(self) -> None:
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM quality_checks")
            conn.execute("DELETE FROM reply_records")
            conn.execute("DELETE FROM knowledge_hits")
            conn.execute("DELETE FROM intent_results")
            conn.execute("DELETE FROM follow_up_tasks")
            conn.execute("DELETE FROM conversation_tags")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM conversations")

    def ensure_conversation(self, conversation_id: str | None, shop_id: str, user_id: str) -> dict[str, Any]:
        conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
        current_time = now_iso()

        with get_connection() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE conversations SET last_message_at = ? WHERE id = ?",
                    (current_time, conversation_id),
                )
                return dict(row)

            conn.execute(
                """
                INSERT INTO conversations (id, platform, shop_id, user_id, status, current_intent, risk_level, owner_id, last_message_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    "xiaohongshu",
                    shop_id,
                    user_id,
                    "open",
                    None,
                    "low",
                    None,
                    current_time,
                    current_time,
                ),
            )
        return {
            "id": conversation_id,
            "platform": "xiaohongshu",
            "shop_id": shop_id,
            "user_id": user_id,
            "status": "open",
            "current_intent": None,
            "risk_level": "low",
            "owner_id": None,
            "last_message_at": current_time,
            "created_at": current_time,
        }

    def add_message(
        self,
        conversation_id: str,
        sender_type: str,
        content: str,
        message_type: str,
        product_id: str | None,
        order_context: dict[str, Any] | None,
        logistics_context: dict[str, Any] | None,
    ) -> str:
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (id, conversation_id, sender_type, content, message_type, product_id, order_context_json, logistics_context_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    sender_type,
                    content,
                    message_type,
                    product_id,
                    json.dumps(order_context or {}, ensure_ascii=False),
                    json.dumps(logistics_context or {}, ensure_ascii=False),
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE conversations SET last_message_at = ? WHERE id = ?",
                (now_iso(), conversation_id),
            )
        return message_id

    def add_intent_result(self, message_id: str, intent: str, confidence: float, signals: list[str]) -> str:
        intent_id = f"int_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO intent_results (id, message_id, intent, confidence, signals_json, model_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    message_id,
                    intent,
                    confidence,
                    json.dumps(signals, ensure_ascii=False),
                    "rule-classifier-v1",
                    now_iso(),
                ),
            )
        return intent_id

    def add_knowledge_hits(self, message_id: str, hits: list[dict[str, Any]]) -> list[str]:
        hit_ids: list[str] = []
        with get_connection() as conn:
            for hit in hits:
                hit_id = f"hit_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO knowledge_hits (id, message_id, kb_type, doc_id, score, title, snippet, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hit_id,
                        message_id,
                        hit["kb_type"],
                        hit["doc_id"],
                        hit["score"],
                        hit["title"],
                        hit["content"],
                        now_iso(),
                    ),
                )
                hit_ids.append(hit_id)
        return hit_ids

    def add_reply_record(
        self,
        message_id: str,
        draft_reply: str,
        final_reply: str | None,
        reply_status: str,
        prompt_template: str,
        model_name: str,
        cited_knowledge_ids: list[str],
        risk_notes: list[str],
    ) -> str:
        reply_id = f"rep_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reply_records (id, message_id, draft_reply, final_reply, reply_status, prompt_template, model_name, cited_knowledge_ids_json, risk_notes_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reply_id,
                    message_id,
                    draft_reply,
                    final_reply,
                    reply_status,
                    prompt_template,
                    model_name,
                    json.dumps(cited_knowledge_ids, ensure_ascii=False),
                    json.dumps(risk_notes, ensure_ascii=False),
                    now_iso(),
                ),
            )
        return reply_id

    def add_quality_check(
        self,
        reply_id: str,
        passed: bool,
        risk_level: str,
        issues: list[dict[str, Any]],
        suggestion: str,
        review_mode: str,
    ) -> str:
        quality_id = f"qc_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO quality_checks (id, reply_id, pass, risk_level, issues_json, suggestion, review_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quality_id,
                    reply_id,
                    int(passed),
                    risk_level,
                    json.dumps(issues, ensure_ascii=False),
                    suggestion,
                    review_mode,
                    now_iso(),
                ),
            )
        return quality_id

    def replace_tags(self, conversation_id: str, tags: list[str]) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM conversation_tags WHERE conversation_id = ?", (conversation_id,))
            for tag in tags:
                conn.execute(
                    """
                    INSERT INTO conversation_tags (id, conversation_id, tag_code, tag_source, active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (f"tag_{uuid.uuid4().hex[:12]}", conversation_id, tag, "system", 1, now_iso()),
                )

    def create_follow_up_task(self, conversation_id: str, reason: str, priority: str) -> str:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        due_at = (datetime.now(UTC) + timedelta(hours=4 if priority == "P1" else 12)).isoformat()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO follow_up_tasks (id, conversation_id, reason, priority, status, assignee_id, due_at, created_at, resolved_at, resolution_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, conversation_id, reason, priority, "open", None, due_at, now_iso(), None, None),
            )
            conn.execute(
                "UPDATE conversations SET status = ?, risk_level = ? WHERE id = ?",
                ("pending_review", "high" if priority == "P1" else "medium", conversation_id),
            )
        return task_id

    def update_conversation(self, conversation_id: str, current_intent: str, risk_level: str, status: str) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE conversations
                SET current_intent = ?, risk_level = ?, status = ?, last_message_at = ?
                WHERE id = ?
                """,
                (current_intent, risk_level, status, now_iso(), conversation_id),
            )

    def get_recent_history(self, conversation_id: str, limit: int = 6) -> list[str]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT content FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [row["content"] for row in reversed(rows)]

    def get_conversation_detail(self, conversation_id: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            conversation = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not conversation:
                return None

            def fetch_all(query: str) -> list[dict[str, Any]]:
                rows = conn.execute(query, (conversation_id,)).fetchall()
                return [dict(row) for row in rows]

            messages = fetch_all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC")
            intents = fetch_all(
                """
                SELECT ir.* FROM intent_results ir
                JOIN messages m ON m.id = ir.message_id
                WHERE m.conversation_id = ?
                ORDER BY ir.created_at ASC
                """
            )
            knowledge_hits = fetch_all(
                """
                SELECT kh.* FROM knowledge_hits kh
                JOIN messages m ON m.id = kh.message_id
                WHERE m.conversation_id = ?
                ORDER BY kh.created_at ASC
                """
            )
            replies = fetch_all(
                """
                SELECT rr.* FROM reply_records rr
                JOIN messages m ON m.id = rr.message_id
                WHERE m.conversation_id = ?
                ORDER BY rr.created_at ASC
                """
            )
            quality_checks = fetch_all(
                """
                SELECT qc.* FROM quality_checks qc
                JOIN reply_records rr ON rr.id = qc.reply_id
                JOIN messages m ON m.id = rr.message_id
                WHERE m.conversation_id = ?
                ORDER BY qc.created_at ASC
                """
            )
            tags = fetch_all("SELECT * FROM conversation_tags WHERE conversation_id = ? ORDER BY created_at ASC")
            tasks = fetch_all("SELECT * FROM follow_up_tasks WHERE conversation_id = ? ORDER BY created_at ASC")

        return {
            "conversation": dict(conversation),
            "messages": messages,
            "intents": intents,
            "knowledge_hits": knowledge_hits,
            "replies": replies,
            "quality_checks": quality_checks,
            "tags": tags,
            "follow_up_tasks": tasks,
        }

    def delete_conversation(self, conversation_id: str) -> bool:
        with get_connection() as conn:
            conversation = conn.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not conversation:
                return False

            conn.execute(
                """
                DELETE FROM quality_checks
                WHERE reply_id IN (
                    SELECT rr.id
                    FROM reply_records rr
                    JOIN messages m ON m.id = rr.message_id
                    WHERE m.conversation_id = ?
                )
                """,
                (conversation_id,),
            )
            conn.execute(
                """
                DELETE FROM reply_records
                WHERE message_id IN (
                    SELECT id FROM messages WHERE conversation_id = ?
                )
                """,
                (conversation_id,),
            )
            conn.execute(
                """
                DELETE FROM knowledge_hits
                WHERE message_id IN (
                    SELECT id FROM messages WHERE conversation_id = ?
                )
                """,
                (conversation_id,),
            )
            conn.execute(
                """
                DELETE FROM intent_results
                WHERE message_id IN (
                    SELECT id FROM messages WHERE conversation_id = ?
                )
                """,
                (conversation_id,),
            )
            conn.execute("DELETE FROM follow_up_tasks WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversation_tags WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return True

    def list_conversations(self, status: str | None = None, risk_level: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT
                c.*,
                (
                    SELECT m.content
                    FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC
                    LIMIT 1
                ) AS latest_message
            FROM conversations c
        """
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("c.status = ?")
            params.append(status)
        if risk_level:
            conditions.append("c.risk_level = ?")
            params.append(risk_level)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY c.last_message_at DESC LIMIT ?"
        params.append(limit)

        with get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                tag_rows = conn.execute(
                    "SELECT tag_code FROM conversation_tags WHERE conversation_id = ? AND active = 1 ORDER BY created_at ASC",
                    (item["id"],),
                ).fetchall()
                item["active_tags"] = [row["tag_code"] for row in tag_rows]
        return items

    def get_dashboard_metrics(self) -> dict[str, int]:
        with get_connection() as conn:
            total_conversations = conn.execute("SELECT COUNT(1) AS count FROM conversations").fetchone()["count"]
            pending_review = conn.execute(
                "SELECT COUNT(1) AS count FROM conversations WHERE status = 'pending_review'"
            ).fetchone()["count"]
            high_risk = conn.execute(
                "SELECT COUNT(1) AS count FROM conversations WHERE risk_level = 'high'"
            ).fetchone()["count"]
            open_tasks = conn.execute(
                "SELECT COUNT(1) AS count FROM follow_up_tasks WHERE status = 'open'"
            ).fetchone()["count"]
            claimed_tasks = conn.execute(
                "SELECT COUNT(1) AS count FROM follow_up_tasks WHERE status = 'claimed'"
            ).fetchone()["count"]
            auto_reply_count = conn.execute(
                "SELECT COUNT(1) AS count FROM quality_checks WHERE review_mode = 'auto_pass'"
            ).fetchone()["count"]
            sent_reply_count = conn.execute(
                "SELECT COUNT(1) AS count FROM reply_records WHERE reply_status = 'sent'"
            ).fetchone()["count"]
            blocked_reply_count = conn.execute(
                "SELECT COUNT(1) AS count FROM quality_checks WHERE review_mode = 'blocked'"
            ).fetchone()["count"]

        return {
            "total_conversations": total_conversations,
            "pending_review_conversations": pending_review,
            "open_follow_up_tasks": open_tasks,
            "claimed_follow_up_tasks": claimed_tasks,
            "high_risk_conversations": high_risk,
            "auto_reply_count": auto_reply_count,
            "sent_reply_count": sent_reply_count,
            "blocked_reply_count": blocked_reply_count,
        }

    def get_system_config(self) -> dict[str, Any]:
        self.ensure_default_config()
        with get_connection() as conn:
            row = conn.execute("SELECT value_json FROM system_configs WHERE key = ?", ("agent_settings",)).fetchone()
        config = json.loads(row["value_json"]) if row else {}
        return {**self._default_config(), **config}

    def update_system_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.ensure_default_config()
        config = self.get_system_config()
        config.update({key: value for key, value in patch.items() if value is not None})
        with get_connection() as conn:
            conn.execute(
                "UPDATE system_configs SET value_json = ?, updated_at = ? WHERE key = ?",
                (json.dumps(config, ensure_ascii=False), now_iso(), "agent_settings"),
            )
        return config

    def list_follow_up_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM follow_up_tasks"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY due_at ASC, created_at ASC"

        with get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def claim_task(self, task_id: str, assignee_id: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            task = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                return None
            conn.execute(
                "UPDATE follow_up_tasks SET status = ?, assignee_id = ? WHERE id = ?",
                ("claimed", assignee_id, task_id),
            )
            updated = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(updated) if updated else None

    def resolve_task(self, task_id: str, resolution_note: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            task = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                return None
            resolved_at = now_iso()
            conn.execute(
                "UPDATE follow_up_tasks SET status = ?, resolved_at = ?, resolution_note = ? WHERE id = ?",
                ("resolved", resolved_at, resolution_note, task_id),
            )
            conn.execute(
                """
                UPDATE conversations
                SET status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM follow_up_tasks
                        WHERE conversation_id = ? AND status IN ('open', 'claimed')
                    ) THEN status
                    ELSE 'open'
                END
                WHERE id = ?
                """,
                (task["conversation_id"], task["conversation_id"]),
            )
            updated = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(updated) if updated else None
