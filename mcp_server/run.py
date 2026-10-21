"""MCP server 本地启动入口：python run.py（9000 端口）

跟 backend/run.py 同一个坑：Windows 下手动建 SelectorEventLoop 再 serve，
别直接 python -m uvicorn（新 uvicorn 自建循环不理会全局 policy）。
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    config = uvicorn.Config("main:app", port=9000, log_level="info")
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()
