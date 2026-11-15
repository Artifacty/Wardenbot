"""Persistent, revisioned jobs for language and clan roles (database layer only)."""
ALLOWED = {'language': {'ru', 'en'}, 'clan': {'leader', 'delegate', 'none'}}


def enqueue(conn, guild_id, user_id, scope, target):
    if target not in ALLOWED.get(scope, set()):
        raise ValueError('Invalid role assignment')
    conn.execute('INSERT INTO aux_role_jobs(guild_id,user_id,scope,target) VALUES (?,?,?,?) '
                 'ON CONFLICT(guild_id,user_id,scope) DO UPDATE SET target=excluded.target, '
                 'revision=aux_role_jobs.revision+1, last_error=NULL', (guild_id,user_id,scope,target))


def get(conn, guild_id, user_id, scope):
    return conn.execute('SELECT * FROM aux_role_jobs WHERE guild_id=? AND user_id=? AND scope=?',
                        (guild_id,user_id,scope)).fetchone()


def pending(conn):
    return conn.execute('SELECT * FROM aux_role_jobs WHERE applied_revision<revision').fetchall()


def acknowledge(conn, job):
    conn.execute('UPDATE aux_role_jobs SET applied_revision=?, last_error=NULL '
                 'WHERE guild_id=? AND user_id=? AND scope=? AND revision=?',
                 (job['revision'],job['guild_id'],job['user_id'],job['scope'],job['revision']))


def failed(conn, job, error):
    conn.execute('UPDATE aux_role_jobs SET last_error=? '
                 'WHERE guild_id=? AND user_id=? AND scope=? AND revision=?',
                 (error,job['guild_id'],job['user_id'],job['scope'],job['revision']))
