"""Durable public operation snapshots. Private briefings are deliberately excluded."""
import json
from datetime import UTC, datetime
from . import repo


def pending(conn):
    return conn.execute("SELECT o.* FROM operations o LEFT JOIN operation_archives a ON a.op_id=o.id "
                        "WHERE o.status IN ('done','cancelled') AND COALESCE(a.source_deleted,0)=0 "
                        "AND NOT EXISTS(SELECT 1 FROM notifications n WHERE n.op_id=o.id AND n.state='pending') ORDER BY o.id").fetchall()


def get(conn, op_id):
    return conn.execute('SELECT * FROM operation_archives WHERE op_id=?', (op_id,)).fetchone()


def save(conn, op_id, channel_id, payload):
    conn.execute('INSERT OR IGNORE INTO operation_archives(op_id,channel_id,payload) VALUES (?,?,?)',
                 (op_id, channel_id, json.dumps(payload, ensure_ascii=False)))


def receipt(conn, op_id, message_id):
    conn.execute('UPDATE operation_archives SET message_id=? WHERE op_id=?', (message_id,op_id))


def complete(conn, op_id):
    changed=conn.execute('UPDATE operation_archives SET source_deleted=1 WHERE op_id=? AND source_deleted=0', (op_id,)).rowcount
    if changed:
        op=repo.op_get(conn,op_id)
        repo.audit_add(conn,op['guild_id'],None,'Архивирование операции',str(op_id),'Публичный снимок сохранён, исходный пост удалён',datetime.now(UTC))
