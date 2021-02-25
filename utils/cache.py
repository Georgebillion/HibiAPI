import hashlib
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple, TypeVar

from aiocache import Cache as AioCache  # type: ignore
from aiocache.base import BaseCache  # type:ignore
from pydantic import BaseModel
from pydantic.decorator import ValidatedFunction

from .config import Config
from .log import logger

_AsyncCallable = TypeVar("_AsyncCallable", bound=Callable[..., Coroutine])


class CacheConfig(BaseModel):
    endpoint: Callable[..., Coroutine]
    namespace: str
    enabled: bool = True
    ttl: timedelta = timedelta(seconds=Config["cache"]["ttl"].as_number())

    @staticmethod
    def new(function: Callable[..., Coroutine]):
        return CacheConfig(endpoint=function, namespace=function.__qualname__)


def cache_config(
    enabled: bool = True,
    ttl: timedelta = timedelta(hours=1),
    namespace: Optional[str] = None,
):
    def decorator(endpoint: _AsyncCallable) -> _AsyncCallable:
        setattr(
            endpoint,
            "cache_config",
            CacheConfig(
                endpoint=endpoint,
                namespace=namespace or endpoint.__qualname__,
                enabled=enabled,
                ttl=ttl,
            ),
        )
        return endpoint

    return decorator


disable_cache = cache_config(enabled=False)


class CachedValidatedFunction(ValidatedFunction):
    def serialize(self, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> BaseModel:
        values = self.build_values(args=args, kwargs=kwargs)
        return self.model(**values)


def endpoint_cache(function: _AsyncCallable) -> _AsyncCallable:
    from .routing import request_headers, response_headers  # noqa:F401

    vf = CachedValidatedFunction(function)
    cache: BaseCache = AioCache.from_url(Config["cache"]["uri"].as_str())  # type:ignore
    config: CacheConfig = getattr(function, "cache_config", CacheConfig.new(function))

    cache.namespace, cache.ttl = config.namespace, config.ttl.total_seconds()

    @wraps(function)
    async def wrapper(*args, **kwargs):
        cache_policy: str = request_headers.get().get("cache-control", "public")

        if (not config.enabled) or (cache_policy.casefold() == "no-store"):
            return await vf.call(*args, **kwargs)

        key = hashlib.md5(
            (model := vf.serialize(args=args, kwargs=kwargs))
            .json(exclude={"self"}, sort_keys=True, ensure_ascii=False)
            .encode()
        ).hexdigest()

        if cache_policy.casefold() == "no-cache":
            await cache.delete(key)

        if await cache.exists(key):
            logger.debug(
                f"Request to endpoint <g>{function.__qualname__}</g> "
                f"restoring from <e>{key=}</e> in cache data."
            )
            result, cache_date = await cache.get(key)
        else:
            result, cache_date = await vf.execute(model), datetime.now()
            await cache.set(key, (result, cache_date))

        max_age = cache_date + timedelta(seconds=cache.ttl) - datetime.now()
        if max_age.total_seconds() > 0:
            response_headers.get().setdefault(
                "Cache-Control",
                "max-age=%d" % max_age.total_seconds(),
            )

        return result

    return wrapper  # type:ignore