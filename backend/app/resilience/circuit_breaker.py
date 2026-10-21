"""三态熔断器：CLOSED 正常干活 -> OPEN 直接拒 -> 冷却完 HALF_OPEN 放一个探测"""
import time
from enum import Enum


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, name: str, max_failures: int = 3, cooldown: float = 60.0):
        self.name = name
        self.max_failures = max_failures
        self.cooldown = cooldown
        self.state = BreakerState.CLOSED
        self.failures = 0
        self.opened_at = 0.0

    def allow(self) -> bool:
        """要不要放行这次调用"""
        if self.state == BreakerState.CLOSED:
            return True
        if self.state == BreakerState.OPEN:
            if time.monotonic() - self.opened_at >= self.cooldown:
                self.state = BreakerState.HALF_OPEN
                return True  # 放一个探测请求试试水
            return False
        return True  # HALF_OPEN：只放探测的那个

    def on_success(self) -> None:
        self.failures = 0
        self.state = BreakerState.CLOSED

    def on_failure(self) -> None:
        self.failures += 1
        if self.state == BreakerState.HALF_OPEN or self.failures >= self.max_failures:
            self.state = BreakerState.OPEN
            self.opened_at = time.monotonic()
