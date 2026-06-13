"""Process marker — write PID file for debugging, no blocking.

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK._process_lock import write_pid_marker
    write_pid_marker(output_dir, city_name)  # just writes PID, never blocks
"""

import atexit
import os


def write_pid_marker(output_dir: str, city_name: str) -> None:
    """Write a PID marker file. Never blocks, always overwrites.

    Creates a .pid_{city_name} file in output_dir containing the PID.
    This is informational only — it does NOT prevent concurrent execution.
    Automatically removes the marker on process exit.
    """
    pid_file = os.path.join(output_dir, f".pid_{city_name}")
    my_pid = os.getpid()

    os.makedirs(output_dir, exist_ok=True)

    # Always overwrite — force acquire
    with open(pid_file, 'w') as f:
        f.write(str(my_pid))

    # Clean up on exit
    def _cleanup():
        try:
            if os.path.exists(pid_file):
                with open(pid_file, 'r') as f:
                    pid_in_file = int(f.read().strip())
                if pid_in_file == my_pid:
                    os.remove(pid_file)
        except (ValueError, OSError):
            pass

    atexit.register(_cleanup)


# Backward compat: old scripts call acquire_lock()
acquire_lock = write_pid_marker
