"""A process lock per database, including Windows. The lock file itself is retained."""
from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def instance_lock(db_path):
    if db_path == ':memory:':
        yield
        return
    path=Path(db_path).resolve().with_suffix(Path(db_path).suffix+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    acquired=False
    try:
        if os.name=='nt':
            import msvcrt
            if path.stat().st_size==0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise RuntimeError('Другой экземпляр бота уже использует эту базу. Остановите его перед запуском.') from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('Другой экземпляр бота уже использует эту базу. Остановите его перед запуском.') from exc
        acquired=True
        yield
    finally:
        if acquired:
            if os.name=='nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
        handle.close()
