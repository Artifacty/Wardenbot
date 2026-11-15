"""Durable public operation snapshots. Private briefings are deliberately excluded."""
import json


def pending(conn):
    return conn.execute("SELECT o.* FROM operations o LEFT JOIN operation_archives a ON a.op_id=o.id "
                        "WHERE o.status IN ('done','cancelled') AND COALESCE(a.source_deleted,0)=0 "
                        "ORDER BY o.id").fetchall()


def get(conn, op_id):
    return conn.execute('SELECT * FROM operation_archives WHERE op_id=?', (op_id,)).fetchone()


def save(conn, op_id, channel_id, payload):
    conn.execute('INSERT OR IGNORE INTO operation_archives(op_id,channel_id,payload) VALUES (?,?,?)',
                 (op_id, channel_id, json.dumps(payload, ensure_ascii=False)))


def receipt(conn, op_id, message_id):
    conn.execute('UPDATE operation_archives SET message_id=? WHERE op_id=?', (message_id,op_id))


def complete(conn, op_id):
    conn.execute('UPDATE operation_archives SET source_deleted=1 WHERE op_id=?', (op_id,))
