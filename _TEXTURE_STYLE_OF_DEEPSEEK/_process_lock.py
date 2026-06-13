"""Process lock — prevent multiple concurrent pipeline instances.

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK._process_lock import acquire_lock
    acquire_lock(output_dir, city_name)  # exits if another instance is running
"""

import atexit
import os
import sys


def acquire_lock(output_dir: str, city_name: str) -> None:
    """Acquire a process lock. Exits if another instance is already running.

    Creates a .lock_{city_name} file in output_dir containing the PID.
    Automatically removes the lock on process exit (via atexit).
    """
    lock_file = os.path.join(output_dir, f".lock_{city_name}")
    my_pid = os.getpid()

    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                other_pid = int(f.read().strip())

            # Check if the other process is still running
            other_alive = _is_process_alive(other_pid)

            if other_alive:
                print(f"\n[LOCK] Another instance (PID {other_pid}) is already "
                      f"running {city_name}. Exiting.")
                sys.exit(0)
            else:
                print(f"\n[LOCK] Removing stale lock from PID {other_pid}")
                os.remove(lock_file)
        except (ValueError, OSError):
            # Corrupted lock file, remove it
            try:
                os.remove(lock_file)
            except OSError:
                pass

    # Write our PID
    os.makedirs(output_dir, exist_ok=True)
    with open(lock_file, 'w') as f:
        f.write(str(my_pid))

    # Clean up lock on exit
    def _cleanup():
        try:
            if os.path.exists(lock_file):
                with open(lock_file, 'r') as f:
                    pid_in_file = int(f.read().strip())
                if pid_in_file == my_pid:
                    os.remove(lock_file)
        except (ValueError, OSError):
            pass

    atexit.register(_cleanup)


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if os.name == 'nt':
        # Windows: use tasklist
        import subprocess
        try:
            flags = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=5,
                creationflags=flags)
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        # Unix: send signal 0
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
