# utils/locks.py
import asyncio

_JOB_LOCKS: dict[str, asyncio.Lock] = {}

def get_job_lock(job_id: str) -> asyncio.Lock:
    """
    Get or create a unique asyncio.Lock for a specific job_id.
    Ensures serialization of background task execution per job.
    """
    if job_id not in _JOB_LOCKS:
        _JOB_LOCKS[job_id] = asyncio.Lock()
    return _JOB_LOCKS[job_id]
