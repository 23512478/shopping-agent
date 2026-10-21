"""盯价调度器：APScheduler 常驻后台。

调度任务是进程内的，服务一重启内存里就啥都没了，所以启动时会从库里把订阅读回来
重新挂上（见 restore_monitors）——盯价不该因为重启就悄悄停掉。
"""
import logging
import os

from app.correlation import set_cid
from app.memory import working
from app.resilience.tool_gateway import call_tool_resilient

logger = logging.getLogger(__name__)

_scheduler = None


def start_scheduler() -> None:
    global _scheduler
    if os.getenv("MONITOR_ENABLED", "1") == "0":
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        _scheduler = AsyncIOScheduler()
        _scheduler.start()
        logger.info("盯价调度器已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning("调度器启动失败，盯价功能降级: %s", e)
        return
    restore_monitors()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _interval_minutes() -> int:
    return int(os.getenv("MONITOR_INTERVAL_MIN", "60"))


def _add_job(
    session_id: str,
    product_ids: list[str],
    threshold,
    user_id,
    baseline: dict | None,
    drop_percent=None,
) -> None:
    """把周期查价任务挂上去（同一个会话先摘旧的，别叠成两个）。

    baseline 传的是个可变 dict，check_prices 会往里补「第一轮没基准」的商品，
    补完下一轮就直接用得上，不用再读库。drop_percent 是百分比降幅，和绝对目标价
    二选一，交给 check_prices 折算触发线。
    """
    job_id = f"monitor:{session_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    _scheduler.add_job(
        check_prices,
        "interval",
        minutes=_interval_minutes(),
        id=job_id,
        args=[session_id, product_ids, threshold, user_id, baseline or {}, drop_percent],
    )


def restore_monitors() -> int:
    """服务启动时把库里所有订阅重新挂成周期任务，返回挂上了几条。

    以前只靠用户再说一句「盯着」才恢复，人不说话盯价就一直是停的。库里本来就存着
    商品、阈值和基准价，启动时照着挂回来即可；顺手沿用存的那份基准价，重启前后
    价格没动就不会误报「降价」。库连不上或调度器没起就安静跳过，别拦服务启动。
    """
    if not _scheduler:
        return 0
    try:
        from app.memory import monitors

        subs = monitors.list_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅读取失败，重启恢复跳过: %s", e)
        return 0
    n = 0
    for s in subs:
        pids = s.get("product_ids") or []
        if not pids:
            continue
        _add_job(
            s.get("session_id"),
            pids,
            s.get("threshold"),
            s.get("user_id"),
            s.get("baseline"),
            s.get("drop_percent"),
        )
        n += 1
    if n:
        logger.info("盯价任务已从库里恢复 %s 条", n)
    return n


def is_registered(session_id: str) -> bool:
    """这个会话的盯价任务当前是否真在跑。

    调度任务是进程内的，服务重启后内存里啥都没了——启动时会从库恢复（见
    restore_monitors），恢复不上的（库连不上之类的）这里就返回 False，
    订阅记录也不能再显示「监控中」（active 要照实反映，不能撒谎）。
    """
    return bool(_scheduler and _scheduler.get_job(f"monitor:{session_id}"))


def unregister_monitor(session_id: str) -> bool:
    """取消盯价：把内存里的周期任务摘掉；本来就没任务（重启后没恢复上）返回 False。"""
    if not _scheduler:
        return False
    job_id = f"monitor:{session_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        return True
    return False


def register_monitor(
    user_id,
    session_id: str,
    product_ids: list[str],
    threshold,
    baseline: dict | None = None,
    drop_percent=None,
) -> bool:
    """注册周期查价任务，并把订阅落库（个人主页「我的盯价」看这份记录）。

    调度器没起或没商品就返回 False（降级）——但订阅记录仍然写，
    免得服务重启后「我的盯价」列表空掉。baseline 是注册那一刻每件商品的价格，
    查价时只有跌破它才算降价（见 check_prices）；同一条订阅重新注册时会沿用
    库里存的那份旧 baseline，不会拿「这次注册时的价」当新基线（见 _baseline_for）。

    threshold 是绝对目标价，drop_percent 是百分比降幅，两者二选一：用户说
    「降价 10% 提醒我」就走 drop_percent，触发线由 check_prices 按基准价折算。
    """
    if not product_ids:
        return False
    base, same_watch = _baseline_for(session_id, product_ids, threshold, baseline, drop_percent)
    _save_subscription(user_id, session_id, product_ids, threshold, base, drop_percent)
    if not _scheduler:
        return False
    # 只有换了商品或换了目标价/降幅（新一轮盯价）才清去重记录，让新一轮重新提醒；
    # 同一条订阅重新注册时去重也留着——baseline 沿用了旧值，清掉的话刚重启
    # 就会把之前已经推过的降价再推一遍。
    if not same_watch:
        working.clear_notify_dedup(session_id)
    _add_job(session_id, product_ids, threshold, user_id, base, drop_percent)
    return True


def _same_num(a, b) -> bool:
    """两个目标口径是不是同一个：库里的 numeric 取回来是 float，别拿 500 和 500.0 较劲。"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def _baseline_for(
    session_id: str, product_ids: list[str], threshold, fresh: dict | None, drop_percent=None
) -> tuple[dict | None, bool]:
    """定这份基准价，顺带告出「是不是同一条订阅又注册了一次」。

    服务重启后调度任务全没了，用户再盯一次时会发现库里早记着基准价：商品和目标价
    （或降幅）都没变就沿用旧基准（中间这段的降价才不会被「重启那一刻的价」抹平）；
    换了商品或目标口径就是新一轮盯价，拿这次查到的价重新记基准。库里没有的商品
    （新加进来的）也用这次查到的价。
    """
    fresh = fresh or {}
    try:
        from app.memory import monitors

        old = monitors.get_monitor_by_session(session_id)
    except Exception as e:  # noqa: BLE001 - 读不到就当新订阅，别拦注册
        logger.warning("盯价订阅读取失败，这轮按新基准盯: %s", e)
        old = None
    same_watch = (
        bool(old)
        and _same_num(old.get("threshold"), threshold)
        and _same_num(old.get("drop_percent"), drop_percent)
        and sorted(old.get("product_ids") or []) == sorted(product_ids)
    )
    kept = ((old or {}).get("baseline") or {}) if same_watch else {}
    base: dict[str, float] = {}
    for pid in product_ids:
        if isinstance(kept.get(pid), (int, float)):
            base[pid] = float(kept[pid])
        elif isinstance(fresh.get(pid), (int, float)):
            base[pid] = float(fresh[pid])
    return (base or None), same_watch


def _save_subscription(
    user_id, session_id, product_ids, threshold, baseline=None, drop_percent=None
) -> None:
    try:
        from app.memory import monitors

        monitors.upsert_monitor(
            session_id=session_id,
            product_ids=product_ids,
            threshold=threshold,
            user_id=user_id,
            baseline=baseline,
            drop_percent=drop_percent,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价订阅落库失败: %s", e)


def _lenient_assert(data: dict):
    """盯价对数据宽容：查不到下轮再说，不重试不进死信"""
    return True, ""


async def check_prices(
    session_id: str,
    product_ids: list[str],
    threshold,
    user_id: str | None = None,
    baseline: dict | None = None,
    drop_percent=None,
) -> None:
    # 后台任务没有请求上下文，不设的话 cid 会残留上一条请求的值，grep 会看串线
    set_cid(f"monitor-{session_id[:8]}")
    data, err = await call_tool_resilient(
        "price_query",
        {"product_ids": product_ids},
        node_id="monitor",
        degrade={"prices": []},
        assert_fn=_lenient_assert,
    )
    if err:
        return  # 降级了，这轮不记录也不提醒
    rows = data.get("prices", [])
    # 每次查价都留一份进价格历史，盯价页才画得出走势（同价不会重复写）
    _record_history(rows)
    base = baseline if baseline is not None else {}
    seeded = False
    for row in rows:
        price = row.get("price")
        pid = row.get("product_id")
        if pid is None or not isinstance(price, (int, float)):
            continue
        mark = base.get(pid)
        if mark is None:
            # 还没基准价（老订阅没这一列，或者注册那会儿查不到价）：这一轮只把现价记成
            # 基准，不提醒。不这么做的话，「本来就卖 9.8 的商品 + 阈值 20」这种订阅在
            # 重启恢复调度后第一轮就推一条「已到 ¥9.8（阈值 ¥20）」——一件都没降，纯噪音。
            base[pid] = float(price)
            seeded = True
            continue
        # 还得真的比「基准那一刻的价」更低才算降价。只看「现价 <= 阈值」的话，
        # 用户说「这些耳机降到 500 叫我」而它们本来就卖 9.8，注册完第一轮
        # 就把整批推一遍「已到 ¥9.8（阈值 ¥500）」——一件都没降，纯噪音。
        # 用户真正想要的是「比现在便宜就叫我」，阈值只是他随口给的那条线。
        #
        # 触发线有两种口径：给了绝对目标价就用它；只说「降价 10% 提醒我」时
        # threshold 是空的，就按基准价折算——跌破「基准价 × (1 - 10%)」才算数。
        line = threshold
        if line is None and drop_percent:
            line = mark * (1 - float(drop_percent) / 100.0)
        if not line or price > line or price >= mark:
            continue
        # 先真入队再打日志：以前不看结果就打，被去重拦下时日志照样刷一屏
        # 「降价提醒」，看着像推了 30 条，其实一条都没进队列
        pushed = working.push_notify(
            session_id,
            {
                "type": "price_drop",
                **row,
                "threshold": threshold,
                "drop_percent": drop_percent,
                "prev_price": mark,
            },
            user_id,
        )
        if pushed:
            logger.warning(
                "降价提醒 session=%s %s 已到 %s（阈值 %s / 降幅 %s%%，基准 %s）",
                session_id, pid, price, threshold, drop_percent, mark,
            )
    if seeded:
        _save_baseline(session_id, base)


def _save_baseline(session_id: str, baseline: dict) -> None:
    """把补记的基准价写回订阅，下次重启恢复时基准就还在。"""
    try:
        from app.memory import monitors

        monitors.set_baseline(session_id, baseline)
    except Exception as e:  # noqa: BLE001
        logger.warning("盯价基准写回失败: %s", e)


def _record_history(rows: list[dict]) -> None:
    try:
        from app.memory import price_history

        price_history.record_prices(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("价格历史落库失败: %s", e)
