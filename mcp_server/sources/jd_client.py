"""京东联盟网关客户端：MD5 签名 + routerjson POST。

凭证来自导购媒体的 appkey/secretkey（联盟后台「导购媒体管理」里发的那对），
从环境变量 JD_APP_KEY / JD_SECRET_KEY 读，没配就是不可用，上层回落 mock。

签名规则（宙斯网关）：secret + 所有参数按 key 字典序拼 key+value + secret → MD5 大写。
业务参数塞在 360buy_param_json 里；网关层 code=0 只代表受理，业务结果在内层 JSON 字符串。
"""
import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request

logger = logging.getLogger("mcp_server.jd")

GATEWAY = "https://api.jd.com/routerjson"


def _now_gmt8() -> str:
    """京东网关要求时间戳按 **GMT+8**；容器默认时区是 UTC，直接 localtime 会差 8 小时，
    被网关判「时间戳参数与服务器时间差异大」直接拒掉——所以这里显式按 GMT+8 出。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))


class JDApiError(Exception):
    """业务层报错（无权限、限流、参数错），带上业务 code 方便定位。"""


class JDClient:
    def __init__(self, app_key: str, secret: str, timeout: float = 10.0):
        self.app_key = app_key
        self.secret = secret
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "JDClient | None":
        app_key = os.getenv("JD_APP_KEY", "").strip()
        secret = os.getenv("JD_SECRET_KEY", "").strip()
        if not app_key or not secret:
            return None
        return cls(app_key, secret)

    def _sign(self, params: dict) -> str:
        raw = self.secret + "".join(f"{k}{params[k]}" for k in sorted(params)) + self.secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

    def call(self, method: str, biz: dict) -> dict:
        """调一个联盟接口，返回解析后的业务层 dict；业务报错抛 JDApiError。"""
        params = {
            "method": method,
            "app_key": self.app_key,
            "timestamp": _now_gmt8(),
            "format": "json",
            "v": "1.0",
            "sign_method": "md5",
            "360buy_param_json": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
        }
        params["sign"] = self._sign(params)
        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(GATEWAY, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            outer = json.loads(resp.read().decode("utf-8"))

        if "error_response" in outer:
            err = outer["error_response"]
            raise JDApiError(f"网关拒绝 {method}: {err.get('zh_desc') or err}")

        for v in outer.values():
            if not isinstance(v, dict):
                continue
            inner = v.get("queryResult") or v.get("getResult") or v.get("result")
            if inner is None:
                continue
            inner = json.loads(inner) if isinstance(inner, str) else inner
            code = inner.get("code")
            if code not in (0, 200, "0", "200"):
                raise JDApiError(f"{method} 业务code={code} {inner.get('message', '')}")
            return inner
        raise JDApiError(f"{method} 返回结构不认识: {str(outer)[:200]}")

    def jingfen_query(self, elite_id: int, page: int, size: int = 50) -> list[dict]:
        """京粉精选频道拉一页商品。code=200 但 data 为空也算正常（频道没货）。"""
        r = self.call(
            "jd.union.open.goods.jingfen.query",
            {"goodsReq": {"eliteId": elite_id, "pageIndex": page, "pageSize": size}},
        )
        return r.get("data") or []
