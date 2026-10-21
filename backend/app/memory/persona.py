"""Long-Term Persona：用户偏好/品牌倾向/预算习惯，Chroma 向量检索，prefTopK=6。

Chroma 没起就静默降级，别拦主流程。
"""
import logging
import os

logger = logging.getLogger(__name__)

_collection = None
_FAIL = object()


def _get_collection():
    global _collection
    if _collection is None:
        try:
            import chromadb

            client = chromadb.HttpClient(
                host=os.getenv("CHROMA_HOST", "localhost"),
                port=int(os.getenv("CHROMA_PORT", "8001")),
            )
            client.heartbeat()
            _collection = client.get_or_create_collection("persona")
        except Exception as e:  # noqa: BLE001
            logger.warning("Chroma 连不上，用户画像降级为空: %s", e)
            _collection = _FAIL
    return None if _collection is _FAIL else _collection


def add_persona(user_id: str, text: str, doc_id: str) -> None:
    col = _get_collection()
    if not col:
        return
    try:
        col.add(ids=[doc_id], documents=[text], metadatas=[{"user_id": user_id}])
    except Exception as e:  # noqa: BLE001
        logger.warning("画像写入失败: %s", e)


def query_persona(user_id: str, query: str, top_k: int = 6) -> list[str]:
    col = _get_collection()
    if not col:
        return []
    try:
        res = col.query(
            query_texts=[query],
            n_results=top_k,
            where={"user_id": user_id},
        )
        return (res.get("documents") or [[]])[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("画像检索失败: %s", e)
        return []


def list_persona(user_id: str, limit: int = 20) -> list[str]:
    """把某用户沉淀的全部偏好取出来（个人主页展示用，不走相关性检索）。"""
    col = _get_collection()
    if not col:
        return []
    try:
        res = col.get(where={"user_id": user_id}, limit=limit)
        return res.get("documents") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("画像列表读取失败: %s", e)
        return []
