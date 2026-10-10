"""A process must own the database before it starts market/Telegram workers."""
import os
from pathlib import Path


class DatabaseLease:
    def __init__(self, database):
        self.path = str(Path(database).expanduser().resolve()) + "-lock"
        self.descriptor = None

    def __enter__(self):
        self.descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.descriptor).st_size == 0:
                    os.write(self.descriptor, b"0")
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.descriptor)
            self.descriptor = None
            raise ValueError("База уже используется другой копией бота; остановите её или выберите отдельную базу") from None
        return self

    def __exit__(self, *_):
        if self.descriptor is not None:
            # Closing releases the OS lock; do not unlink a shared lock inode.
            os.close(self.descriptor)
            self.descriptor = None
