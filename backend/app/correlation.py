"""correlation_id 全链路追踪：API 层 set，节点/工具 get，日志自动带 [cid]"""
import contextvars
import logging

cid_var: contextvars.ContextVar = contextvars.ContextVar("correlation_id", default="-")


def set_cid(cid: str) -> None:
    cid_var.set(cid)


def get_cid() -> str:
    return cid_var.get()


class CidFilter(logging.Filter):
    def filter(self, record):
        record.cid = get_cid()
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(CidFilter())
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] [%(cid)s] %(name)s %(levelname)s %(message)s",
            "%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
