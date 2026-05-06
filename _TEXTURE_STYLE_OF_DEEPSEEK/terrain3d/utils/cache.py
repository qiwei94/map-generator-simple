"""TTL-based cache management utilities."""

import glob
import logging
import os
import time

logger = logging.getLogger(__name__)

# Default TTL: 3 days
DEFAULT_TTL = 3 * 24 * 3600


def is_valid(path: str, ttl_seconds: int = DEFAULT_TTL) -> bool:
    """Check if a cache file exists and is within TTL.

    Args:
        path: path to the cache file
        ttl_seconds: max age in seconds (negative = never expire, 0 = always expired)

    Returns:
        True if file exists and within TTL (or TTL is negative)
    """
    if not os.path.exists(path):
        return False
    if ttl_seconds < 0:
        return True
    if ttl_seconds == 0:
        return False
    age = time.time() - os.path.getmtime(path)
    return age < ttl_seconds


def get_age(path: str) -> float:
    """Return age of a cache file in seconds. Returns -1 if not found."""
    if not os.path.exists(path):
        return -1
    return time.time() - os.path.getmtime(path)


def format_age(path: str) -> str:
    """Return a human-readable age string for a cache file."""
    age = get_age(path)
    if age < 0:
        return "not cached"
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def invalidate(path: str) -> bool:
    """Delete a cache file. Returns True if file existed."""
    if os.path.exists(path):
        os.remove(path)
        logger.debug(f"Invalidated cache: {path}")
        return True
    return False


def clear_expired(cache_dir: str, ttl_seconds: int = DEFAULT_TTL,
                  pattern: str = "*") -> int:
    """Remove all expired files in a cache directory.

    Args:
        cache_dir: directory to scan
        ttl_seconds: files older than this are removed
        pattern: glob pattern for files to check

    Returns:
        Number of files removed
    """
    if not os.path.isdir(cache_dir):
        return 0

    removed = 0
    now = time.time()
    for filepath in glob.glob(os.path.join(cache_dir, pattern)):
        if os.path.isfile(filepath):
            age = now - os.path.getmtime(filepath)
            if age >= ttl_seconds:
                os.remove(filepath)
                removed += 1

    if removed > 0:
        logger.info(f"Cleared {removed} expired cache files from {cache_dir}")
    return removed
