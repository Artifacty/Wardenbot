"""Transactional ticket and immediate-group services, independent of Discord."""
from . import repo
from .operation_service import OperationError


def require_access(conn, uid, staff=False):
    user = repo.user_get(conn, uid)
    if not staff and (not user or user['status'] != 'admitted'):
        raise OperationError('Сначала пройдите проверку.')


def validate(title, body, capacity):
    if not title.strip() or not body.strip() or not 1 <= capacity <= 20:
        raise OperationError('Укажите название, описание и количество участников от 1 до 20.')
    if len(title) > 70 or len(body) > 1000:
        raise OperationError('Сократите название до 70 символов, описание — до 1000.')


def create(conn, guild, owner, kind, title, body, capacity=1, staff=False, request_key=None, ticket_type="question"):
    require_access(conn, owner, staff)
    if kind not in ('ticket', 'group'):
        raise OperationError('Неизвестный тип записи.')
    validate(title, body, capacity)
    if ticket_type not in ('question', 'complaint'):
        raise OperationError('Выберите вопрос или жалобу.')
    if request_key:
        prior = conn.execute('SELECT id, guild_id, owner_id FROM community_posts WHERE request_key=?', (request_key,)).fetchone()
        if prior:
            if prior['guild_id'] != guild or prior['owner_id'] != owner:
                raise OperationError('Запись недоступна.')
            return prior['id']
    count = conn.execute("SELECT COUNT(*) FROM community_posts WHERE guild_id=? AND owner_id=? AND kind=? AND state='open'", (guild, owner, kind)).fetchone()[0]
    if count >= (1 if kind == 'ticket' else 2):
        raise OperationError('У вас уже есть открытый тикет или две активные группы.')
    pid = conn.execute('INSERT INTO community_posts(guild_id,owner_id,kind,title,body,capacity,request_key,ticket_type) VALUES (?,?,?,?,?,?,?,?)',
                       (guild, owner, kind, title.strip(), body.strip(), capacity, request_key, ticket_type)).lastrowid
    if kind == 'group':
        conn.execute('INSERT INTO group_members VALUES (?,?)', (pid, owner))
    return pid


def get(conn, pid):
    return conn.execute('SELECT * FROM community_posts WHERE id=?', (pid,)).fetchone()


def members(conn, pid):
    return [r[0] for r in conn.execute('SELECT user_id FROM group_members WHERE post_id=? ORDER BY user_id', (pid,))]


def manageable(conn, pid, guild, uid, staff):
    row = get(conn, pid)
    if not row or row['guild_id'] != guild or row['state'] != 'open':
        raise OperationError('Запись уже закрыта или недоступна.')
    if not staff and uid != row['owner_id']:
        raise OperationError('Управлять может автор или модератор.')
    return row


def edit(conn, pid, guild, uid, staff, title, body, capacity):
    require_access(conn, uid, staff)
    row = manageable(conn, pid, guild, uid, staff)
    if row['kind'] != 'group':
        raise OperationError('Это не группа.')
    validate(title, body, capacity)
    if capacity < len(members(conn, pid)):
        raise OperationError('Мест не может быть меньше, чем уже записалось участников.')
    conn.execute('UPDATE community_posts SET title=?,body=?,capacity=? WHERE id=?', (title.strip(), body.strip(), capacity, pid))


def act(conn, pid, guild, uid, staff, action):
    row = get(conn, pid)
    if not row or row['guild_id'] != guild:
        raise OperationError('Запись недоступна.')
    if action == 'close':
        if not staff and uid != row['owner_id']:
            raise OperationError('Закрыть может автор или модератор.')
        conn.execute("UPDATE community_posts SET state='closed' WHERE id=?", (pid,))
        return
    if row['state'] != 'open':
        raise OperationError('Группа уже завершена.')
    if row['kind'] != 'group' or action not in ('join', 'leave'):
        raise OperationError('Неизвестное действие группы.')
    if action == 'leave':
        if uid == row['owner_id']:
            raise OperationError('Организатор может завершить группу.')
        conn.execute('DELETE FROM group_members WHERE post_id=? AND user_id=?', (pid, uid))
        return
    require_access(conn, uid, staff)
    users = members(conn, pid)
    if uid in users:
        return
    if len(users) >= row['capacity']:
        raise OperationError('Все места заняты.')
    conn.execute('INSERT INTO group_members VALUES (?,?)', (pid, uid))


def reconcile_access(conn, pid, staff_ids):
    """Release revoked participants' places and close a revoked owner's group."""
    row = get(conn, pid)
    if not row or row['kind'] != 'group' or row['state'] != 'open':
        return
    for uid in members(conn, pid):
        user = repo.user_get(conn, uid)
        if uid not in staff_ids and (not user or user['status'] != 'admitted'):
            if uid == row['owner_id']:
                conn.execute("UPDATE community_posts SET state='closed' WHERE id=?", (pid,))
            else:
                conn.execute('DELETE FROM group_members WHERE post_id=? AND user_id=?', (pid, uid))
