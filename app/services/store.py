from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.database import get_connection, init_db
from app.core.config import INTENT_PROMPTS, LLM_API_KEY, PROMISE_RISK_PATTERNS


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AgentStore:
    def _find_follow_up_source_message(self, conn, conversation_id: str, task_created_at: str, message_id: str | None):
        # 旧任务可能没有 message_id，这里尽量定位到最接近的用户消息，
        # 方便客服看到触发人工跟进的原始对话。
        if message_id:
            direct = conn.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if direct:
                return direct

        source_message = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE conversation_id = ? AND sender_type = 'user' AND created_at <= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id, task_created_at),
        ).fetchone()
        if source_message:
            return source_message

        return conn.execute(
            """
            SELECT *
            FROM messages
            WHERE conversation_id = ? AND sender_type = 'user'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()

    def _default_config(self) -> dict[str, Any]:
        return {
            "auto_reply_enabled": True,
            "intent_confidence_threshold": 0.7,
            "quality_block_on_sensitive_missing_kb": True,
            "prompts": INTENT_PROMPTS,
            "promise_risk_patterns": PROMISE_RISK_PATTERNS,
            "llm_enabled": True,
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

    def ensure_conversation(self, conversation_id: str | None, shop_id: str, user_id: str) -> dict[str, Any]:
        conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
        current_time = now_iso()

        with get_connection() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE conversations SET shop_id = ?, user_id = ?, last_message_at = ? WHERE id = ?",
                    (shop_id, user_id, current_time, conversation_id),
                )
                refreshed = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
                return dict(refreshed) if refreshed else dict(row)

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

    def create_follow_up_task(self, conversation_id: str, message_id: str, reason: str, priority: str) -> str:
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT * FROM follow_up_tasks
                WHERE conversation_id = ? AND message_id = ? AND status IN ('open', 'claimed')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (conversation_id, message_id),
            ).fetchone()

            due_at = (datetime.now(UTC) + timedelta(hours=4 if priority == "P1" else 12)).isoformat()
            if existing:
                conn.execute(
                    """
                    UPDATE follow_up_tasks
                    SET reason = ?, priority = ?, due_at = ?
                    WHERE id = ?
                    """,
                    (reason, priority, due_at, existing["id"]),
                )
                conn.execute(
                    "UPDATE conversations SET status = ?, risk_level = ? WHERE id = ?",
                    ("pending_review", "high" if priority == "P1" else "medium", conversation_id),
                )
                return existing["id"]

            task_id = f"task_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO follow_up_tasks (id, conversation_id, message_id, reason, priority, status, assignee_id, due_at, created_at, resolved_at, resolution_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, conversation_id, message_id, reason, priority, "open", None, due_at, now_iso(), None, None),
            )
            conn.execute(
                "UPDATE conversations SET status = ?, risk_level = ? WHERE id = ?",
                ("pending_review", "high" if priority == "P1" else "medium", conversation_id),
            )
        return task_id

    def cleanup_duplicate_follow_up_tasks(self) -> int:
        removed = 0
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, message_id, status, created_at
                FROM follow_up_tasks
                WHERE status IN ('open', 'claimed')
                ORDER BY conversation_id ASC, message_id ASC, created_at DESC
                """
            ).fetchall()

            keep_by_message: dict[tuple[str, str | None], str] = {}
            for row in rows:
                dedupe_key = (
                    row["conversation_id"],
                    row["message_id"] if row["message_id"] is not None else row["id"],
                )
                # 没有关联消息的任务按唯一任务处理；有关联消息时，同一会话同一消息
                # 只保留最新的 open/claimed 任务。
                if dedupe_key not in keep_by_message:
                    keep_by_message[dedupe_key] = row["id"]
                    continue
                conn.execute("DELETE FROM follow_up_tasks WHERE id = ?", (row["id"],))
                removed += 1
        return removed

    def backfill_follow_up_task_message_ids(self) -> int:
        updated = 0
        with get_connection() as conn:
            tasks = conn.execute(
                """
                SELECT id, conversation_id, created_at
                FROM follow_up_tasks
                WHERE message_id IS NULL
                ORDER BY conversation_id ASC, created_at ASC
                """
            ).fetchall()

            used_message_ids = {
                row["message_id"]
                for row in conn.execute("SELECT message_id FROM follow_up_tasks WHERE message_id IS NOT NULL").fetchall()
            }

            for task in tasks:
                # 回填时优先选任务创建前最近的一条用户消息；如果已被其他任务占用，
                # 再退而选择时间最接近且未使用的消息。
                candidate = conn.execute(
                    """
                    SELECT id
                    FROM messages
                    WHERE conversation_id = ? AND sender_type = 'user' AND created_at <= ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (task["conversation_id"], task["created_at"]),
                ).fetchone()

                if not candidate:
                    candidate = conn.execute(
                        """
                        SELECT id
                        FROM messages
                        WHERE conversation_id = ? AND sender_type = 'user'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (task["conversation_id"],),
                    ).fetchone()

                if not candidate:
                    continue

                candidate_id = candidate["id"]
                if candidate_id in used_message_ids:
                    candidate = conn.execute(
                        """
                        SELECT id
                        FROM messages
                        WHERE conversation_id = ? AND sender_type = 'user' AND id NOT IN (
                            SELECT message_id FROM follow_up_tasks WHERE message_id IS NOT NULL
                        )
                        ORDER BY ABS(strftime('%s', created_at) - strftime('%s', ?)) ASC, created_at ASC
                        LIMIT 1
                        """,
                        (task["conversation_id"], task["created_at"]),
                    ).fetchone()
                    if not candidate:
                        continue
                    candidate_id = candidate["id"]

                conn.execute(
                    "UPDATE follow_up_tasks SET message_id = ? WHERE id = ?",
                    (candidate_id, task["id"]),
                )
                used_message_ids.add(candidate_id)
                updated += 1

        return updated

    def restore_missing_follow_up_tasks(self, confidence_threshold: float = 0.7) -> int:
        restored = 0
        with get_connection() as conn:
            candidates = conn.execute(
                """
                SELECT
                    m.id AS message_id,
                    m.conversation_id,
                    m.created_at AS message_created_at,
                    c.risk_level AS conversation_risk_level,
                    rr.reply_status,
                    rr.prompt_template,
                    rr.created_at AS reply_created_at,
                    COALESCE(ir.confidence, 1.0) AS confidence,
                    EXISTS(
                        SELECT 1
                        FROM knowledge_hits kh
                        WHERE kh.message_id = m.id
                    ) AS has_knowledge
                FROM messages m
                JOIN reply_records rr ON rr.message_id = m.id
                LEFT JOIN intent_results ir ON ir.message_id = m.id
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.sender_type = 'user'
                ORDER BY m.created_at ASC, rr.created_at ASC
                """
            ).fetchall()

            for candidate in candidates:
                has_task = conn.execute(
                    "SELECT 1 FROM follow_up_tasks WHERE message_id = ? LIMIT 1",
                    (candidate["message_id"],),
                ).fetchone()
                if has_task:
                    continue

                needs_follow_up = (
                    candidate["reply_status"] == "pending_review"
                    or candidate["prompt_template"] == "human_handoff_template"
                )
                if not needs_follow_up:
                    continue

                # 这条修复路径保持保守：只有持久化的回复状态已经证明需要人工复核时，
                # 才补建待跟进任务。
                reason_parts: list[str] = []
                if candidate["confidence"] < confidence_threshold:
                    reason_parts.append("低置信度识别")
                if not candidate["has_knowledge"]:
                    reason_parts.append("知识未命中")
                reason = "；".join(reason_parts) if reason_parts else "需要人工跟进"
                priority = "P1" if candidate["conversation_risk_level"] == "high" else "P2"
                due_at = (datetime.now(UTC) + timedelta(hours=4 if priority == "P1" else 12)).isoformat()

                conn.execute(
                    """
                    INSERT INTO follow_up_tasks (
                        id, conversation_id, message_id, reason, priority, status,
                        assignee_id, due_at, created_at, resolved_at, resolution_note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"task_{uuid.uuid4().hex[:12]}",
                        candidate["conversation_id"],
                        candidate["message_id"],
                        reason,
                        priority,
                        "open",
                        None,
                        due_at,
                        candidate["reply_created_at"] or candidate["message_created_at"] or now_iso(),
                        None,
                        None,
                    ),
                )
                restored += 1

        return restored

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
        merged = {**self._default_config(), **config}
        merged["llm_api_key_configured"] = bool(LLM_API_KEY.strip())
        return merged

    def update_system_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.ensure_default_config()
        config = self.get_system_config()
        config.update(
            {
                key: value
                for key, value in patch.items()
                if value is not None and key != "llm_api_key_configured"
            }
        )
        config.pop("llm_api_key_configured", None)
        with get_connection() as conn:
            conn.execute(
                "UPDATE system_configs SET value_json = ?, updated_at = ? WHERE key = ?",
                (json.dumps(config, ensure_ascii=False), now_iso(), "agent_settings"),
            )
        return self.get_system_config()

    def list_follow_up_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT
                fut.*,
                c.user_id,
                c.current_intent,
                COALESCE(
                    m.content,
                    (
                        SELECT m2.content
                        FROM messages m2
                        WHERE m2.conversation_id = fut.conversation_id
                          AND m2.sender_type = 'user'
                          AND m2.created_at <= fut.created_at
                        ORDER BY m2.created_at DESC
                        LIMIT 1
                    ),
                    (
                        SELECT m3.content
                        FROM messages m3
                        WHERE m3.conversation_id = fut.conversation_id AND m3.sender_type = 'user'
                        ORDER BY m3.created_at DESC
                        LIMIT 1
                    )
                ) AS message_content
            FROM follow_up_tasks fut
            LEFT JOIN conversations c ON c.id = fut.conversation_id
            LEFT JOIN messages m ON m.id = fut.message_id
        """
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE fut.status = ?"
            params = (status,)
        else:
            query += " WHERE fut.status IN ('open', 'claimed')"
        query += " ORDER BY fut.due_at ASC, fut.created_at ASC"

        with get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_follow_up_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            task = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                return None

            conversation = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (task["conversation_id"],),
            ).fetchone()
            source_message = self._find_follow_up_source_message(
                conn,
                task["conversation_id"],
                task["created_at"],
                task["message_id"],
            )
            latest_reply = conn.execute(
                """
                SELECT rr.*
                FROM reply_records rr
                JOIN messages m ON m.id = rr.message_id
                WHERE (
                    rr.message_id = ?
                    OR (? IS NULL AND m.conversation_id = ?)
                )
                ORDER BY rr.created_at DESC
                LIMIT 1
                """,
                (task["message_id"], task["message_id"], task["conversation_id"]),
            ).fetchone()
            latest_quality = None
            if latest_reply:
                latest_quality = conn.execute(
                    "SELECT * FROM quality_checks WHERE reply_id = ? ORDER BY created_at DESC LIMIT 1",
                    (latest_reply["id"],),
                ).fetchone()

        task_dict = dict(task)
        if source_message and not task_dict.get("message_id"):
            task_dict["message_id"] = source_message["id"]
        task_dict["message_content"] = source_message["content"] if source_message else None

        return {
            "task": task_dict,
            "conversation": dict(conversation) if conversation else None,
            "source_message": dict(source_message) if source_message else None,
            "latest_reply": dict(latest_reply) if latest_reply else None,
            "latest_quality_check": dict(latest_quality) if latest_quality else None,
        }

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

    def resolve_task_with_manual_reply(self, task_id: str, manual_reply: str, resolution_note: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            task = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                return None

            conversation_id = task["conversation_id"]
            target_message_id = task["message_id"]
            if target_message_id:
                reply_id = f"rep_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO reply_records (id, message_id, draft_reply, final_reply, reply_status, prompt_template, model_name, cited_knowledge_ids_json, risk_notes_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reply_id,
                        target_message_id,
                        manual_reply,
                        manual_reply,
                        "manual_sent",
                        "manual_follow_up",
                        "human-agent",
                        json.dumps([], ensure_ascii=False),
                        json.dumps(["人工处理回复"], ensure_ascii=False),
                        now_iso(),
                    ),
                )

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
                    ) THEN 'pending_review'
                    ELSE 'open'
                END,
                risk_level = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM follow_up_tasks
                        WHERE conversation_id = ? AND status IN ('open', 'claimed')
                    ) THEN risk_level
                    ELSE 'low'
                END
                WHERE id = ?
                """,
                (conversation_id, conversation_id, conversation_id),
            )
            updated = conn.execute("SELECT * FROM follow_up_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(updated) if updated else None

    def clear_open_follow_up_tasks(self) -> int:
        with get_connection() as conn:
            rows = conn.execute("SELECT id, conversation_id FROM follow_up_tasks WHERE status IN ('open', 'claimed')").fetchall()
            removed = len(rows)
            for row in rows:
                conn.execute("DELETE FROM follow_up_tasks WHERE id = ?", (row["id"],))
                conn.execute(
                    """
                    UPDATE conversations
                    SET status = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM follow_up_tasks WHERE conversation_id = ? AND status IN ('open', 'claimed')
                        ) THEN status
                        ELSE 'open'
                    END
                    WHERE id = ?
                    """,
                    (row["conversation_id"], row["conversation_id"]),
                )
        return removed
