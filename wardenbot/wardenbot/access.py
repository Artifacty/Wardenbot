"""Access withdrawal cancels open applications and removes future operation access atomically."""
from . import repo, role_jobs
from .timeutil import to_iso


def revoke(conn,guild_id,user_id,actor_id,reason,now,banned=False):
    conn.execute("UPDATE operations SET card_revision=card_revision+1 WHERE guild_id=? AND status IN ('open','running')",(guild_id,))
    repo.user_update(conn,user_id,now,status='banned' if banned else 'new',rank=None,last_reject_at=None)
    conn.execute("UPDATE verification_requests SET status='rejected',decided_by=?,decided_at=?,reason=? "
                 "WHERE guild_id=? AND user_id=? AND status IN ('pending','awaiting_screenshot','needs_info')",
                 (actor_id,to_iso(now),reason,guild_id,user_id))
    conn.execute("DELETE FROM op_signups WHERE user_id=? AND op_id IN (SELECT id FROM operations WHERE guild_id=? AND status IN ('open','running'))",(user_id,guild_id))
    conn.execute("UPDATE supply_pledges SET amount=delivered WHERE user_id=? AND op_id IN (SELECT id FROM operations WHERE guild_id=? AND status IN ('open','running'))",(user_id,guild_id))
    repo.role_job_set(conn,guild_id,user_id,'unverified')
    role_jobs.enqueue(conn,guild_id,user_id,'clan','none')
    repo.audit_add(conn,guild_id,actor_id,'Отзыв допуска',str(user_id),reason,now)


def new_war(conn,guild_id,number,user_ids,actor_id,now):
    old=repo.cfg_get(conn,guild_id,'war.current')
    if old and int(old)>=number:
        raise ValueError('Номер новой войны должен быть больше текущего.')
    for uid in user_ids:
        user=repo.user_get(conn,uid)
        if user and user['status']!='banned':
            revoke(conn,guild_id,uid,actor_id,f'Повторная проверка: война {number}',now)
    repo.cfg_set(conn,guild_id,'war.current',number)
    return len(user_ids)
