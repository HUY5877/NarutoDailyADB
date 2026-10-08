"""每次模型 HTTP 尝试通过 Redis INCR 选择渠道，不启动额外代理服务。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from threading import Lock
from typing import Mapping
from urllib.parse import urlsplit
from urllib.request import Request

import httpx2 as httpx
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.asyncio.retry import Retry as AsyncRetry

from naruto_daily.common.environment import EnvironmentConfigError, read_environment
from .channels import CHANNELS, Channel, channel_values


LOGGER = logging.getLogger('naruto_daily')
ROUTING_COUNTER_KEY = 'naruto:multimodal:route:sequence'
REDIS_TIMEOUT_SECONDS = 5


class GatewayRoutingError(OSError):
    """Redis 计数失败；传输层可在原有预算内重试，禁止切换到本地计数。"""


@dataclass(frozen=True)
class GatewaySettings:
    channels: tuple[Channel, ...]
    redis_url: str = field(repr=False)


def load_gateway_settings(values: Mapping[str, str | None] | None = None) -> GatewaySettings:
    values = read_environment() if values is None else values
    active = []
    for definition in CHANNELS:
        key, model = channel_values(values, definition)
        if not key:
            continue
        if not model:
            raise EnvironmentConfigError(f'请配置有效渠道 {definition.name} 的 {definition.model_variable}')
        active.append(Channel(definition.name, definition.base_url, model, key))
    if not active:
        raise EnvironmentConfigError('未配置有效多模态渠道，请在根目录 .env 配置 '
                                     'YUWAN_API_KEY 或 DEEPSEEK_API_KEY')
    redis_url = (values.get('REDIS_URL') or '').strip()
    try:
        address = urlsplit(redis_url)
        if address.scheme not in ('redis', 'rediss') or not address.hostname or address.fragment:
            raise ValueError
        if address.port is not None and not 1 <= address.port <= 65535:
            raise ValueError
    except ValueError:
        raise EnvironmentConfigError('多模态路由需要有效的 REDIS_URL（redis:// 或 rediss://）') from None
    return GatewaySettings(tuple(active), redis_url)


def _redis_client(url):
    return Redis.from_url(url, socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
                          socket_timeout=REDIS_TIMEOUT_SECONDS, retry=Retry(NoBackoff(), 0))


def _async_redis_client(url):
    return AsyncRedis.from_url(url, socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
                               socket_timeout=REDIS_TIMEOUT_SECONDS, retry=AsyncRetry(NoBackoff(), 0))


class MultimodalGateway:
    def __init__(self, settings: GatewaySettings):
        self.settings = settings

    @property
    def channels(self):
        return self.settings.channels

    def _channel(self, sequence):
        if type(sequence) is not int or sequence <= 0:
            raise GatewayRoutingError('多模态路由 Redis 计数无效')
        channel = self.channels[sequence % len(self.channels)]
        LOGGER.info('[多模态路由] channel=%s url=%s model=%s sequence=%d active_channels=%d',
                    channel.name, channel.base_url + '/chat/completions', channel.model,
                    sequence, len(self.channels))
        return channel

    def select(self):
        try:
            with _redis_client(self.settings.redis_url) as client:
                sequence = client.incr(ROUTING_COUNTER_KEY)
        except Exception:
            raise GatewayRoutingError('多模态路由 Redis INCR 失败') from None
        return self._channel(sequence)

    async def select_async(self):
        # 每次使用当前事件循环的连接，兼容普通识别的短生命周期 asyncio.run。
        try:
            async with _async_redis_client(self.settings.redis_url) as client:
                sequence = await client.incr(ROUTING_COUNTER_KEY)
        except Exception:
            raise GatewayRoutingError('多模态路由 Redis INCR 失败') from None
        return self._channel(sequence)

    @staticmethod
    def _routed_parts(channel, data, headers):
        payload = json.loads(data)
        payload['model'] = channel.model
        # 官方接口的思考模式不支持强制/指定工具；导航必须保留工具约束。
        choice = payload.get('tool_choice')
        if channel.name == 'deepseek' and (choice == 'required' or isinstance(choice, dict)):
            payload['reasoning_effort'] = 'none'
            payload['thinking'] = {'type': 'disabled'}
        headers = {key: value for key, value in headers.items()
                   if key.lower() not in ('authorization', 'host', 'content-length')}
        headers['Authorization'] = 'Bearer ' + channel.api_key
        return (channel.base_url + '/chat/completions',
                json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers)

    def route_request(self, request: Request) -> Request:
        channel = self.select()
        url, body, headers = self._routed_parts(channel, request.data, dict(request.header_items()))
        return Request(url, data=body, headers=headers, method=request.get_method())

    async def route_async_request(self, request: httpx.Request) -> httpx.Request:
        channel = await self.select_async()
        url, body, headers = self._routed_parts(channel, await request.aread(), dict(request.headers))
        return httpx.Request(request.method, url, content=body, headers=headers, extensions=request.extensions)


_gateway: MultimodalGateway | None = None
_gateway_lock = Lock()


def initialize_gateway() -> MultimodalGateway:
    """进程启动时冻结有效渠道及模型配置；不探测 Redis 或上游 key 有效性。"""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = MultimodalGateway(load_gateway_settings())
            LOGGER.info('多模态网关已初始化：active_channels=%s',
                        ','.join(channel.name for channel in _gateway.channels))
        return _gateway
