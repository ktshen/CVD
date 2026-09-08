from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class ProcessLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.file: BinaryIO | None = None

    def acquire(self, timeout_seconds: float = 0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.1, max(deadline - time.monotonic(), 0)))

    def _try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        self.file = lock_file
        return True

    def release(self) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def process_is_running(lock_path: str | Path) -> bool:
    lock = ProcessLock(lock_path)
    if not lock.acquire():
        return True
    lock.release()
    return False