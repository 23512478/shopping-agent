"""本地启动入口：python run.py

Windows 下 psycopg 异步连接池要求 SelectorEventLoop（默认 Proactor 会报
"Psycopg cannot use the 'ProactorEventLoop' to run in async mode"）。
新版 uvicorn 自建事件循环不理会全局 policy，所以这儿手动建好 Selector
循环再 serve，别直接 python -m uvicorn。Linux/Docker 不受影响。
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    config = uvicorn.Config("app.main:app", port=8000, log_level="info")
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()  # 按上面 policy 建出 SelectorEventLoop
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()
