"""Supervisor：LLM 解析意图 + 关键词兜底 + 指代兜底。LLM 没配或挂了就走关键词，不让主流程死。"""
import logging
import re

from app.graph.state import ShoppingState
from app.llm import LLMNotConfigured, chat, configured, load_prompt, parse_json

logger = logging.getLogger(__name__)

VALID_INTENTS = {"search", "compare", "risk_check", "archive", "monitor", "unknown"}

# 关键词 -> 意图，LLM 兜底用
KEYWORDS = {
    "比价": "compare",
    "便宜": "compare",
    "风险": "risk_check",
    "靠谱": "risk_check",
    "盯价": "monitor",
    "降价": "monitor",
    "归档": "archive",
}

ORDER_WORDS = ("下单", "购买", "买下来", "帮我买")

# 「第一个」「第2款」「这个」「它」这类指代：用户指的是上一轮搜出来的商品。
# 不认出来的话，整句话会被当成品类词塞进 product_search——「帮我下单第一个」
# 八个字里没有品类词，搜出来必然是空，然后就弹一个没商品的空确认框。
_REF_RE = re.compile(r"第\s*([一二三四五1-5])\s*[个款件台只]?|这[个款]|那[个款]|它")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}

# 「降价 10% 提醒我」：相对注册那刻的价跌 X%，跟绝对目标价（「降到 300」）不是一回事。
# 只说「便宜点」没数字不算，必须有百分号。
_DROP_PCT_RE = re.compile(r"(?:降价|降低|降|跌|便宜|低|减)\s*(?:了|价|幅|到|个点|个百分)?\s*(\d+(?:\.\d+)?)\s*[%％]")


def _reference_index(text: str) -> int | None:
    """抠出「指的第几个」：说了序数按序数，只说「这个/它」就默认第一个。

    没有指代返回 None——返回 0 和返回 None 必须分开，不然「这个耳机多少钱」
    这种普通指代会被当成下单指代处理。
    """
    m = _REF_RE.search(text or "")
    if not m:
        return None
    tok = m.group(1)
    if not tok:
        return 1
    return _CN_NUM.get(tok) or int(tok)


def _keyword_route(text: str) -> dict:
    intent = "search"  # 默认就是搜货
    for kw, it in KEYWORDS.items():
        if kw in text:
            intent = it
            break
    # 预算粗解析：LLM 没配时兜底，「预算900」「预算 900」都能认
    m = re.search(r"预算\s*[:：]?\s*(\d+)", text)
    # 目标价粗解析：这是「降到多少告诉我」的触发线，跟预算（我准备花多少）不是一回事
    t = re.search(r"(?:降到|跌到|低到|低于|少于|便宜到)\s*(\d+)", text)
    # 降幅粗解析：「降 10% 提醒我」是相对价跌百分之几，跟绝对目标价分开存
    d = _DROP_PCT_RE.search(text)
    return {
        "intent": intent,
        "category": None,
        "budget": int(m.group(1)) if m else None,
        "target_price": int(t.group(1)) if t else None,
        "drop_percent": float(d.group(1)) if d else None,
        "constraints": [],
        "order_requested": any(w in text for w in ORDER_WORDS),
    }


def _as_number(v):
    """模型偶尔把数字给成字符串（"500"），统一成数字；转不动就当没给。

    不转的话这个值会一路走到 check_prices，和 float 价格比大小直接抛
    TypeError，把后台调度任务搞崩。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _drop_unsaid_brands(constraints: list, text: str) -> list:
    """丢掉用户本次没提过的品牌约束。

    prompt 里已经写死「背景和上文里的品牌不许进 constraints」，但真模型还是会
    偶发地顺手填一个（跑「帮我找个耳机，预算 500」，它填了 ["品牌:纽曼"]，那是
    长期画像里的牌子）。品牌是硬约束，一条就能把 30 件搜成 3 件，所以这里再兜一道：
    品牌名是专有名词，用户真说了就一定能在这句话里找到字面，找不到就当模型自己加的。
    """
    out = []
    for c in constraints or []:
        s = str(c)
        head, sep, value = s.replace("：", ":").partition(":")
        if sep and head.strip() == "品牌":
            brand = value.strip()
            if brand and brand not in text:
                logger.warning("丢掉用户没提过的品牌约束: %s", s)
                continue
        out.append(c)
    return out


async def _llm_route(state: ShoppingState, text: str) -> dict | None:
    """LLM 解析意图，返回 None 表示失败走兜底"""
    if not configured():
        return None
    try:
        sys_prompt = load_prompt("supervisor_intent.md", "系统提示词")
        mem = state.get("memory_context") or {}
        history = mem.get("history") or []
        context_lines = "\n".join(
            f"{h.get('role')}: {h.get('content')}" for h in history[-3:]
        )
        # 长期画像 + 最近会话摘要：只用来理解用户是谁、补全指代和口径，
        # 标签里写死一句「不得据此生成 constraints」——放开了模型会把画像里的
        # 品牌当硬约束，把「帮我找个耳机」从 30 件缩成 3 件
        profile = mem.get("user_profile") or []
        episodes = mem.get("episodes") or []
        memory_lines = ""
        if profile:
            memory_lines += "\n".join(f"- {p}" for p in profile)
        if episodes:
            if memory_lines:
                memory_lines += "\n"
            memory_lines += "\n".join(f"- {e}" for e in episodes[-3:])
        user_content = (
            f"已知用户背景（仅作理解参考，不得据此生成 constraints）：\n{memory_lines}\n\n本次输入: {text}"
            if memory_lines
            else text
        )
        if context_lines:
            user_content = f"{context_lines}\n{user_content}"
        raw = await chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ],
            json_mode=True,
        )
        parsed = parse_json(raw)
        if parsed.get("intent") not in VALID_INTENTS:
            logger.warning("LLM 给了非法意图 %s，走关键词兜底", parsed.get("intent"))
            return None
        return parsed
    except (LLMNotConfigured, Exception) as e:  # noqa: BLE001
        logger.warning("LLM 意图解析失败，走关键词兜底: %s", e)
        return None


async def run(state: ShoppingState) -> dict:
    old_query = state.get("query") or {}
    text = old_query.get("raw", "")
    parsed = await _llm_route(state, text) or _keyword_route(text)
    out = {
        "intent": parsed["intent"],
        "next_node": parsed["intent"],
        # 这三个是「这一轮的结论」，每轮开头先清成干净值：不清的话上一轮的
        # 搜空/跳过会留在 state 里，这轮明明搜到了还跟用户说「没找到」
        "search_empty": False,
        "order_skipped": None,
        "ref_unresolved": False,
        "query": {
            "raw": text,
            "category": parsed.get("category"),
            "budget": parsed.get("budget"),
            # 盯价阈值单独一个字段：以前拿 budget 当阈值，用户说「预算 500」就会被
            # 当成「降到 500 提醒我」，现价本来就没到 500 也照推，等于误报
            "target_price": _as_number(parsed.get("target_price")),
            # 百分比降幅：「降价 10% 提醒我」。和 target_price 二选一，
            # 两个都没给就是「只记价格、不提醒」
            "drop_percent": _as_number(parsed.get("drop_percent")),
            "constraints": _drop_unsaid_brands(parsed.get("constraints"), text),
            "order_requested": bool(parsed.get("order_requested")),
        },
        "completed_nodes": ["supervisor"],
    }

    # 指代兜底：只有「说了要买」才认——「这个耳机多少钱」是普通提问，别乱接。
    # 认出来后直接拿上一轮那件去比价/查风险（跳过搜货），指代就落到具体商品上了。
    if out["query"]["order_requested"]:
        idx = _reference_index(text)
        if idx is not None:
            prev = state.get("product_ids") or []
            if prev:
                out["product_ids"] = [prev[min(idx, len(prev)) - 1]]
                out["next_node"] = "compare"
            else:
                # 上一轮一件都没搜到过，「第一个」无从指起：如实回话，别弹空审批框
                out["next_node"] = "unknown"
                out["ref_unresolved"] = True
    return out
