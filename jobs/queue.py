"""
Redis-backed job queue for fire-and-forget tool execution.

Layout in Redis:
    LIST  jobs_queue              FIFO of pending job JSONs
    HASH  job:{id}                metadata + status + final result
    PUBSUB job_result:{id}        completion event (final result JSON)

Job hash fields:
    tool_name     str
    client        str
    params        JSON
    status        queued | processing | succeeded | failed
    result        JSON (set on terminal states)
    created_at    ISO-8601 UTC
    updated_at    ISO-8601 UTC
    started_at    ISO-8601 UTC (set when worker picks it up)
    finished_at   ISO-8601 UTC (set on terminal states)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from tool_redis import AsyncRedisClient


JOBS_QUEUE_KEY = "jobs_queue"
JOB_TTL_SECONDS = 24 * 60 * 60


def job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def enqueue_job(tool_name: str, client: str, params: Dict[str, Any]) -> str:
    """Push a job onto ``jobs_queue`` and create its status hash. Returns job_id."""
    redis = await AsyncRedisClient.get()

    job_id = str(uuid.uuid4())
    now = _now()
    params_json = json.dumps(params)

    payload = json.dumps({
        "id": job_id,
        "tool_name": tool_name,
        "client": client,
        "params": params,
    })

    pipe = redis.pipeline()
    pipe.hset(job_key(job_id), mapping={
        "tool_name": tool_name,
        "client": client,
        "params": params_json,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
    })
    pipe.expire(job_key(job_id), JOB_TTL_SECONDS)
    pipe.rpush(JOBS_QUEUE_KEY, payload)
    await pipe.execute()

    return job_id


async def read_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Read a job hash. Returns None if it doesn't exist (or expired).

    ``params`` and ``result`` are JSON-decoded for the caller.
    """
    redis = await AsyncRedisClient.get()
    raw = await redis.hgetall(job_key(job_id))
    if not raw:
        return None

    job = dict(raw)
    if "params" in job:
        try:
            job["params"] = json.loads(job["params"])
        except (TypeError, ValueError):
            pass
    if "result" in job:
        try:
            job["result"] = json.loads(job["result"])
        except (TypeError, ValueError):
            pass
    return job


async def mark_processing(job_id: str) -> None:
    redis = await AsyncRedisClient.get()
    now = _now()
    await redis.hset(job_key(job_id), mapping={
        "status": "processing",
        "started_at": now,
        "updated_at": now,
    })


async def mark_terminal(job_id: str, status: str, result: Dict[str, Any]) -> None:
    """Mark a job succeeded or failed, store the result, and publish completion.

    The PUBLISH on ``job_result:{id}`` is a forward-looking hook with no current
    subscriber — the framework's MCPClient polls the hash via job_status_tool.
    Kept so a future push-based completion path (e.g. a hybrid subscribe-then-
    check client, or an admin dashboard watching jobs in real time) can drop
    in without server-side changes. See docs/async-job-queue.md for the
    rationale and the failure modes a push path would need to handle.
    """
    assert status in ("succeeded", "failed"), status
    redis = await AsyncRedisClient.get()
    now = _now()
    result_json = json.dumps(result)

    pipe = redis.pipeline()
    pipe.hset(job_key(job_id), mapping={
        "status": status,
        "result": result_json,
        "finished_at": now,
        "updated_at": now,
    })
    pipe.expire(job_key(job_id), JOB_TTL_SECONDS)
    pipe.publish(f"job_result:{job_id}", result_json)
    await pipe.execute()
