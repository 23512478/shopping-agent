"""指数退避重试：默认最多 3 次，1s/2s/4s 这么等。

base_delay 不传就读环境变量 RETRY_BASE_DELAY，测试里设 0 免得干等。
"""
import asyncio
import os
from functools import wraps


def retry(max_attempts: int = 3, base_delay: float | None = None):
    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            delay = base_delay if base_delay is not None else float(os.getenv("RETRY_BASE_DELAY", "1.0"))
            last_err = None
            for i in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001 - 框架层统一兜底，具体错误往上抛
                    last_err = e
                    if i < max_attempts - 1:
                        await asyncio.sleep(delay * (2 ** i))
            raise last_err

        return wrapper

    return deco
