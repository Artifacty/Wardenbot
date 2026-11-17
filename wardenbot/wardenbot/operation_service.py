"""Transactional operation rules, independent of Discord. Call mutations with Database.write."""
from dataclasses import dataclass
import json
from . import repo, clans
from .opsmeta import OP_TYPES
from .ranks import noshow_block_until
from .timeutil import from_iso, to_iso, resolve_tz


class OperationError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    guild_id: int
    user_id: int
    staff: bool = False
    roles: frozenset[str] = frozenset()


def get(conn, actor, op_id, manage=False, active=True):
    op = repo.op_get(conn, op_id)
    if op is None or op['guild_id'] != actor.guild_id:
        raise OperationError('Операция не найдена на этом сервере.')
    if active and op['status'] not in ('open','running'):
        raise OperationError('Операция уже закрыта.')
    user = repo.user_get(conn, actor.user_id)
    if not actor.staff and (user is None or user['status'] != 'admitted'):
        raise OperationError('Допуск отсутствует или отозван.')
    if manage and not clans.may_manage(conn, actor.guild_id, actor.user_id, op, actor.staff, actor.roles):
        raise OperationError('У вас нет полномочий управлять этой операцией.')
    return op


def notify(conn, op, kind, content, now):
    users = conn.execute('SELECT user_id FROM op_signups WHERE op_id=? UNION SELECT user_id FROM supply_pledges WHERE op_id=? ORDER BY user_id',(op['id'],op['id'])).fetchall()
    conn.execute('INSERT OR IGNORE INTO notifications(op_id,kind,revision,content,created_at,recipients) VALUES (?,?,?,?,?,?)',
                 (op['id'],kind,op['revision'],content,to_iso(now),json.dumps([r['user_id'] for r in users])))


def record(conn, actor, op_id, action, now, details=''):
    repo.audit_add(conn, actor.guild_id, actor.user_id, action, str(op_id), details, now)


def signup(conn, actor, op_id, slot_id, now, limit=3, days=7):
    get(conn,actor,op_id)
    history = repo.attendance_history(conn,actor.user_id,limit)
    until = noshow_block_until(history,limit,days)
    if until and until > now:
        raise OperationError(f'Запись ограничена до {to_iso(until)}.')
    return repo.signup(conn,op_id,actor.user_id,slot_id,now)


def edit(conn, actor, op_id, revision, fields, slots, supply, now):
    op = get(conn,actor,op_id,manage=True)
    if op['revision'] != revision:
        raise OperationError('Операция уже изменена. Откройте форму заново.')
    allowed = {'title','region','type','start_at','duration_min','timezone','requirements','briefing_public','briefing_closed'}
    if set(fields) != allowed:
        raise OperationError('Неполная форма операции.')
    if fields['type'] not in OP_TYPES or not fields['title'].strip():
        raise OperationError('Проверьте название и тип операции.')
    limits={'title':80,'region':40,'requirements':400,'briefing_public':1500,'briefing_closed':1500}
    if any(len(fields[k])>n for k,n in limits.items()) or not 15<=fields['duration_min']<=1440:
        raise OperationError('Превышены ограничения полей или длительности (15 минут — 24 часа).')
    if not slots or len(slots)>12 or len(supply)>12:
        raise OperationError('Нужно 1–12 ролей и не больше 12 видов ресурсов.')
    if (len({x.name.casefold() for x in slots})!=len(slots) or len({x.name.casefold() for x in supply})!=len(supply)
            or any(not 1<=x.capacity<=200 for x in slots) or any(not 1<=x.needed<=10000000 for x in supply)):
        raise OperationError('Проверьте уникальность названий и количества.')
    resolve_tz(fields['timezone'])
    moved = fields['start_at'] != op['start_at']
    if moved and (from_iso(fields['start_at']) <= now or op['status'] != 'open'):
        raise OperationError('Перенести можно только ещё не начатую операцию на будущее время.')
    # Keep IDs by case-insensitive name; removing a populated entry is rejected.
    new_slots = {x.name.casefold(): x for x in slots}
    old_slots = {x['name'].casefold(): x for x in repo.op_slots(conn,op_id)}
    for key, old in old_slots.items():
        count = conn.execute('SELECT COUNT(*) FROM op_signups WHERE slot_id=?',(old['id'],)).fetchone()[0]
        if count and (key not in new_slots or new_slots[key].capacity < count):
            raise OperationError(f"В роли «{old['name']}» уже записано {count}. Нельзя удалить её или уменьшить места ниже записи.")
    new_supply = {x.name.casefold(): x for x in supply}
    old_supply = {x['name'].casefold(): x for x in repo.op_supply(conn,op_id)}
    for key, old in old_supply.items():
        promised = conn.execute('SELECT COALESCE(SUM(amount),0) FROM supply_pledges WHERE supply_id=?',(old['id'],)).fetchone()[0]
        if (promised or old['collected']) and (key not in new_supply or new_supply[key].needed < promised+old['collected']):
            raise OperationError(f"По ресурсу «{old['name']}» уже есть обещания или поставки. Сначала согласуйте их изменение.")
    for key, old in old_slots.items():
        if key not in new_slots:
            conn.execute('DELETE FROM op_slots WHERE id=?',(old['id'],))
    for pos, slot in enumerate(slots):
        if slot.name.casefold() in old_slots:
            conn.execute('UPDATE op_slots SET name=?,capacity=?,position=? WHERE id=?',
                         (slot.name,slot.capacity,pos,old_slots[slot.name.casefold()]['id']))
        else:
            conn.execute('INSERT INTO op_slots(op_id,name,capacity,position) VALUES (?,?,?,?)',(op_id,slot.name,slot.capacity,pos))
    for key, old in old_supply.items():
        if key not in new_supply:
            conn.execute('DELETE FROM supply_pledges WHERE supply_id=?',(old['id'],))
            conn.execute('DELETE FROM op_supply WHERE id=?',(old['id'],))
    for pos, item in enumerate(supply):
        if item.name.casefold() in old_supply:
            conn.execute('UPDATE op_supply SET name=?,needed=?,position=? WHERE id=?',
                         (item.name,item.needed,pos,old_supply[item.name.casefold()]['id']))
        else:
            conn.execute('INSERT INTO op_supply(op_id,name,needed,position) VALUES (?,?,?,?)',(op_id,item.name,item.needed,pos))
    fields = dict(fields,revision=revision+1)
    repo.op_update(conn,op_id,**fields)
    conn.execute("UPDATE notifications SET state='superseded' WHERE op_id=? AND state='pending' AND kind IN ('remind60','remind15')",(op_id,))
    current = repo.op_get(conn,op_id)
    # A new revision does not repeat a reminder already delivered for the same start.
    if not moved:
        conn.execute("INSERT OR IGNORE INTO notifications(op_id,kind,revision,content,state,message_id,created_at) "
                     "SELECT op_id,kind,?,content,'sent',message_id,created_at FROM notifications "
                     "WHERE op_id=? AND revision=? AND state='sent' AND kind IN ('remind60','remind15')",(revision+1,op_id,revision))
    content = f"Операция #{op_id} обновлена. Проверьте публичную карточку."
    if moved:
        content += f" Новое начало: <t:{int(from_iso(current['start_at']).timestamp())}:F>."
    notify(conn,current,'updated',content,now)
    record(conn,actor,op_id,'Изменение операции',now, 'Изменены: '+', '.join(k for k in allowed if op[k] != fields[k]))


def set_recruiting(conn, actor, op_id, opened, now):
    get(conn,actor,op_id,manage=True)
    repo.op_update(conn,op_id,signup_open=int(opened))
    record(conn,actor,op_id,'Изменение набора',now,str(opened))


def cancel(conn, actor, op_id, reason, now):
    get(conn,actor,op_id,manage=True)
    if not reason.strip():
        raise OperationError('Укажите причину отмены.')
    repo.op_cancel(conn,op_id,now)
    repo.op_update(conn,op_id,cancel_reason=reason.strip())
    conn.execute("UPDATE notifications SET state='superseded' WHERE op_id=? AND state='pending' AND kind IN ('remind60','remind15','updated')",(op_id,))
    notify(conn,repo.op_get(conn,op_id),'cancelled',f'Операция #{op_id} отменена. Причина: {reason[:500]}',now)
    record(conn,actor,op_id,'Отмена операции',now,reason[:500])


def finish(conn, actor, op_id, now):
    op = get(conn,actor,op_id,manage=True)
    if op['status'] != 'running' and now < from_iso(op['start_at']):
        raise OperationError('Сначала начните операцию.')
    attendees = repo.op_finish(conn,op_id,now)
    conn.execute("UPDATE notifications SET state='superseded' WHERE op_id=? AND state='pending' AND kind IN ('remind60','remind15','updated')",(op_id,))
    notify(conn,repo.op_get(conn,op_id),'finished',f'Операция #{op_id} завершена. Подтверждённое участие: {len(attendees)}.',now)
    record(conn,actor,op_id,'Завершение операции',now,str(len(attendees)))
    return attendees


def pledge(conn, actor, op_id, supply_id, amount, now):
    get(conn,actor,op_id)
    item = conn.execute('SELECT * FROM op_supply WHERE id=? AND op_id=?',(supply_id,op_id)).fetchone()
    if not item:
        raise OperationError('Ресурс не найден.')
    old = conn.execute('SELECT * FROM supply_pledges WHERE supply_id=? AND user_id=?',(supply_id,actor.user_id)).fetchone()
    delivered = old['delivered'] if old else 0
    if amount < delivered or amount < 0 or amount > 10_000_000:
        raise OperationError('Количество не может быть меньше подтверждённой доставки или вне диапазона 0–10000000.')
    if amount == 0:
        conn.execute('DELETE FROM supply_pledges WHERE supply_id=? AND user_id=?',(supply_id,actor.user_id))
    else:
        conn.execute('INSERT INTO supply_pledges(op_id,supply_id,user_id,amount) VALUES (?,?,?,?) '
                     'ON CONFLICT(supply_id,user_id) DO UPDATE SET amount=excluded.amount', (op_id,supply_id,actor.user_id,amount))
    repo.card_touch(conn,op_id)
    record(conn,actor,op_id,'Обещание поставки',now,f'{supply_id}: {amount}')


def deliver(conn, actor, op_id, supply_id, user_id, amount, now):
    get(conn,actor,op_id,manage=True)
    row = conn.execute('SELECT * FROM supply_pledges WHERE op_id=? AND supply_id=? AND user_id=?',(op_id,supply_id,user_id)).fetchone()
    if not row or not 0 <= amount <= row['amount']:
        raise OperationError('Укажите итоговую доставку от 0 до обещанного количества.')
    conn.execute('UPDATE supply_pledges SET delivered=? WHERE supply_id=? AND user_id=?',(amount,supply_id,user_id))
    repo.card_touch(conn,op_id)
    record(conn,actor,op_id,'Подтверждение доставки',now,f'{user_id}, ресурс {supply_id}: {amount}')


def supply_totals(conn, op_id):
    return conn.execute('SELECT s.*,COALESCE(SUM(p.amount-p.delivered),0) promised, '
                        's.collected+COALESCE(SUM(p.delivered),0) prepared FROM op_supply s '
                        'LEFT JOIN supply_pledges p ON p.supply_id=s.id WHERE s.op_id=? GROUP BY s.id ORDER BY s.position',(op_id,)).fetchall()


def guarded(conn, actor, op_id, fn, args=(), active=True):
    get(conn,actor,op_id,manage=True,active=active)
    return fn(conn,op_id,*args)


def start(conn,actor,op_id,now):
    op=get(conn,actor,op_id,manage=True)
    if op['status']!='open':
        return False
    repo.op_update(conn,op_id,status='running')
    conn.execute("UPDATE notifications SET state='superseded' WHERE op_id=? AND state='pending' AND kind LIKE 'remind%'",(op_id,))
    notify(conn,op,'started',f'Организатор начал операцию #{op_id}.',now)
    record(conn,actor,op_id,'Начало операции',now)
    return True
