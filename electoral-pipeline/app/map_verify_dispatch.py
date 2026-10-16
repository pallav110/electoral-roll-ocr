"""Publish one map-verification job at a time.

Beat is left unchanged so the scheduler that feeds voter OCR does not have
to reload. This process only fills the map-verify queue, and only while that
queue is empty.
"""
from __future__ import annotations

import time

import redis

from app import config
from app.tasks import QUEUE_MAP_VERIFY, verify_map_image


def main() -> None:
    client = redis.Redis.from_url(config.REDIS_URL)
    while True:
        try:
            waiting = int(client.llen(QUEUE_MAP_VERIFY) or 0)
        except redis.RedisError:
            waiting = 1
        if waiting < 1:
            verify_map_image.apply_async(queue=QUEUE_MAP_VERIFY)
        time.sleep(3)


if __name__ == "__main__":
    main()
