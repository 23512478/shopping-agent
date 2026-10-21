"""LLM 客户端：OpenAI 兼容接口。没配 LLM_API_KEY 就当没这个能力，调用方自己兜底。

环境变量：
- LLM_API_KEY   配了才启用 LLM
- LLM_BASE_URL  默认 https://api.openai.com/v1
- LLM_MODEL     默认 gpt-4o-mini
"""
import json
import logging
import os

import httpx

from app.resilience.retry import retry

logger = logging.getLogger(__name__)


class LLMNotConfigured(Exception):
    """没配 LLM_API_KEY，调用方捕获后走降级逻辑"""


def configured() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def load_prompt(filename: str, section: str) -> str:
    """从 backend/app/prompts/xxx.md 里抽指定小节的正文。

    prompt 文件是「给人看的说明」+「给 LLM 的正文」混排，按 '## 小节名' 切。
    """
    path = os.path.join(os.path.dirname(__file__), "prompts", filename)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    marker = f"## {section}"
    if marker not in text:
        return text.strip()
    body = text.split(marker, 1)[1]
    nxt = body.find("\n## ")
    if nxt != -1:
        body = body[:nxt]
    return body.strip()


@retry(max_attempts=2)
async def chat(messages: list[dict], json_mode: bool = False) -> str:
    if not configured():
        raise LLMNotConfigured()
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload: dict = {"model": os.getenv("LLM_MODEL", "gpt-4o-mini"), "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY')}"},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def parse_json(text: str) -> dict:
    """容错解析：剥掉 markdown 代码围栏再试"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:-1]  # 去掉 ``` 和 ``` 收尾
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)
