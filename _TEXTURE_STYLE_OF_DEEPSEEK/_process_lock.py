"""Single-instance guard using OS-level file locking.

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK._process_lock import acquire_lock
    acquire_lock(output_dir, city_name)  # exits if another instance holds the lock
"""

import os
import sys


def acquire_lock(output_dir: str, city_name: str) -> None:
    """Acquire an exclusive file lock. Exits if another instance holds it.

    Uses msvcrt.locking on Windows, fcntl.flock on Unix.
    The lock is automatically released when the process exits.
    """
    os.makedirs(output_dir, exist_ok=True)
    lock_file = os.path.join(output_dir, f".lock_{city_name}")

    try:
        # Open (or create) the lock file
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)

        if os.name == 'nt':
            import msvcrt
            try:
                # Try to lock 1 byte — non-blocking
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                print(f"\n[LOCK] Another instance is already running {city_name}. Exiting.")
                sys.exit(0)
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                print(f"\n[LOCK] Another instance is already running {city_name}. Exiting.")
                sys.exit(0)

        # Write our PID (informational)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())

        # Keep fd open — lock is held until process exits
        # Store fd in module to prevent garbage collection
        _held_locks.append(fd)

    except Exception as e:
        print(f"\n[LOCK] Warning: could not acquire lock: {e}")
        # Don't block execution on lock errors


_held_locks = []
