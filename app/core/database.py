import sqlite3
from contextlib import contextmanager

from app.core.config import DATA_DIR, DB_PATH


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_intent TEXT,
                risk_level TEXT,
                owner_id TEXT,
                last_message_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                content TEXT NOT NULL,
                message_type TEXT NOT NULL,
                product_id TEXT,
                order_context_json TEXT,
                logistics_context_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );

            CREATE TABLE IF NOT EXISTS intent_results (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                confidence REAL NOT NULL,
                signals_json TEXT NOT NULL,
                model_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_hits (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                kb_type TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                score REAL NOT NULL,
                title TEXT NOT NULL,
                snippet TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            );

            CREATE TABLE IF NOT EXISTS reply_records (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                draft_reply TEXT NOT NULL,
                final_reply TEXT,
                reply_status TEXT NOT NULL,
                prompt_template TEXT NOT NULL,
                model_name TEXT NOT NULL,
                cited_knowledge_ids_json TEXT NOT NULL,
                risk_notes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            );

            CREATE TABLE IF NOT EXISTS quality_checks (
                id TEXT PRIMARY KEY,
                reply_id TEXT NOT NULL,
                pass INTEGER NOT NULL,
                risk_level TEXT NOT NULL,
                issues_json TEXT NOT NULL,
                suggestion TEXT,
                review_mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(reply_id) REFERENCES reply_records(id)
            );

            CREATE TABLE IF NOT EXISTS conversation_tags (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                tag_code TEXT NOT NULL,
                tag_source TEXT NOT NULL,
                active INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );

            CREATE TABLE IF NOT EXISTS follow_up_tasks (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                assignee_id TEXT,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );

            CREATE TABLE IF NOT EXISTS system_configs (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
