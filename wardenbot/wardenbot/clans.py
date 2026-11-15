"""Clan registry and authority. All mutations run inside Database.write transactions."""
from . import repo, role_jobs


class ClanError(ValueError):
    pass


def register(conn, guild_id, name):
    name = ' '.join(name.split())
    if not 2 <= len(name) <= 80:
        raise ClanError('name')
    existing = conn.execute('SELECT id FROM clans WHERE guild_id=? AND name_key=?',
                            (guild_id,name.casefold())).fetchone()
    if existing:
        return existing['id']
    return conn.execute('INSERT INTO clans(guild_id,name,name_key) VALUES (?,?,?)',
                        (guild_id,name,name.casefold())).lastrowid


def membership(conn, guild_id, user_id):
    return conn.execute('SELECT m.*,c.name FROM clan_members m JOIN clans c ON c.id=m.clan_id '
                        'WHERE m.guild_id=? AND m.user_id=?', (guild_id,user_id)).fetchone()


def assign(conn, guild_id, actor_id, staff, clan_id, user_id, duty):
    if duty not in ('leader','delegate'):
        raise ClanError('duty')
    clan = conn.execute('SELECT * FROM clans WHERE id=? AND guild_id=?',(clan_id,guild_id)).fetchone()
    if not clan:
        raise ClanError('missing')
    actor = representative(conn,guild_id,actor_id)
    previous = membership(conn,guild_id,user_id)
    if not staff and not (actor and actor['clan_id']==clan_id and actor['duty']=='leader'
                          and duty=='delegate' and (previous is None or
                              (previous['clan_id']==clan_id and previous['duty']=='delegate'))):
        raise ClanError('permission')
    user = repo.user_get(conn,user_id)
    if user is None or user['status'] != 'admitted':
        raise ClanError('unverified')
    if duty == 'leader':
        other = conn.execute("SELECT user_id FROM clan_members WHERE clan_id=? AND duty='leader' AND user_id<>?",
                             (clan_id,user_id)).fetchone()
        if other:
            raise ClanError('leader_exists')
    conn.execute('INSERT INTO clan_members(guild_id,user_id,clan_id,duty) VALUES (?,?,?,?) '
                 'ON CONFLICT(guild_id,user_id) DO UPDATE SET clan_id=excluded.clan_id,duty=excluded.duty',
                 (guild_id,user_id,clan_id,duty))
    role_jobs.enqueue(conn,guild_id,user_id,'clan',duty)


def remove(conn,guild_id,actor_id,staff,user_id):
    target = membership(conn,guild_id,user_id)
    if target is None:
        raise ClanError('missing')
    actor = representative(conn,guild_id,actor_id)
    if not staff and not (actor and actor['duty']=='leader' and actor['clan_id']==target['clan_id']
                          and target['duty']=='delegate'):
        raise ClanError('permission')
    conn.execute('DELETE FROM clan_members WHERE guild_id=? AND user_id=?',(guild_id,user_id))
    role_jobs.enqueue(conn,guild_id,user_id,'clan','none')


def representative(conn,guild_id,user_id):
    user = repo.user_get(conn,user_id)
    return membership(conn,guild_id,user_id) if user and user['status']=='admitted' else None


def may_manage(conn, guild_id, user_id, op, staff, role_keys):
    if op is None or op['guild_id'] != guild_id:
        return False
    if staff:
        return True
    user = repo.user_get(conn,user_id)
    if not user or user['status'] != 'admitted':
        return False
    if op['clan_id'] is None:
        return op['organizer_id']==user_id  # preserve pre-registry operations
    member = representative(conn,guild_id,user_id)
    if not member or member['clan_id'] != op['clan_id']:
        return False
    if member['duty']=='leader' and 'regiment_co' in role_keys:
        return True
    return member['duty']=='delegate' and 'clan_delegate' in role_keys and op['organizer_id']==user_id
