"""
Minimal async Redis client for the MCP server.
Shared by the job queue, the JobWorker and the tools:updated publisher.
No framework dependency.
"""
import os
from typing import Optional

import redis.asyncio as aioredis
from dotenv import load_dotenv

from tool_logger import logger

load_dotenv()


class AsyncRedisClient:
    _instance: Optional[aioredis.Redis] = None

    @classmethod
    async def init(
        cls,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        username: str = None,
        password: str = None,
    ) -> aioredis.Redis:
        cls._instance = aioredis.Redis(
            host=host,
            port=port,
            db=db,
            username=username,
            password=password,
            decode_responses=True,
        )
        await cls._instance.ping()
        logger.debug("Async Redis client initialised.")
        return cls._instance

    @classmethod
    async def get(cls) -> aioredis.Redis:
        if cls._instance is None:
            await cls.init(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=int(os.getenv("REDIS_DB", 0)),
                username=os.getenv("REDIS_USERNAME"),
                password=os.getenv("REDIS_PASSWORD"),
            )
        return cls._instance
