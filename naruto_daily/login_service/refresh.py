"""Read this child's task-scoped command; acknowledgement belongs to the uploader."""

import json
import logging
import time

from naruto_daily.common.namespace import isolate_redis_queue_key
from .models import UUID_PATTERN

LOGGER = logging.getLogger(__name__)


class RefreshRequests:
    def __init__(self, redis_url: str, queue_key: str, task_id: str, *, client=None):
        if not UUID_PATTERN.fullmatch(task_id):
            raise ValueError('Invalid refresh task ID')
        if client is None:
            from redis import Redis
            from redis.backoff import NoBackoff
            from redis.retry import Retry
            client = Redis.from_url(redis_url, decode_responses=True,
                                    socket_connect_timeout=1, socket_timeout=1,
                                    retry=Retry(NoBackoff(), 0))
        self.client = client
        self.task_id = task_id
        self.key = f'{isolate_redis_queue_key(queue_key)}:refresh:{task_id}'
        self.handled = None
        self.next_poll = 0.0
        self.next_warning = 0.0

    def poll(self) -> str | None:
        now = time.monotonic()
        if now < self.next_poll:
            return None
        self.next_poll = now + 1
        try:
            raw = self.client.get(self.key)
            if not raw or len(raw) > 1024:
                return None
            command = json.loads(raw)
            if not isinstance(command, dict) or command.get('taskId') != self.task_id:
                return None
            request_id = command.get('requestId')
            if not isinstance(request_id, str) or not UUID_PATTERN.fullmatch(request_id) or request_id == self.handled:
                return None
            # The parent can take time to upload and acknowledge. Never execute
            # this same request twice while its Redis key is still present.
            self.handled = request_id
            return request_id
        except Exception:
            if now >= self.next_warning:
                LOGGER.warning('暂时无法读取二维码刷新指令，将继续等待扫码并重试读取')
                self.next_warning = now + 30
            return None

    def close(self):
        self.client.close()
