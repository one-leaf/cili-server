"""SQLite schema 兼容：旧库自动补 org_id。"""

from app.db import connect, init_schema


def test_init_schema_adds_org_id_to_old_tables():
    conn = connect(":memory:")
    conn.execute(
        "CREATE TABLE sms_codes ("
        "id TEXT PRIMARY KEY, phone TEXT NOT NULL, code_hash TEXT NOT NULL,"
        "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,"
        "attempts INTEGER NOT NULL DEFAULT 0, ip TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE sms_send_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL,"
        "ip TEXT NOT NULL DEFAULT '', sent_at INTEGER NOT NULL)"
    )
    conn.commit()

    init_schema(conn)

    code_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(sms_codes)")
    }
    event_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(sms_send_events)")
    }
    assert "org_id" in code_columns
    assert "org_id" in event_columns
