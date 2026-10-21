# 多 Agent 电商消费决策系统

面向「买什么、值不值、什么时候买」的导购 Agent：LangGraph 编排 8 个节点，三层记忆承载跨会话偏好，MCP 工具层对接真实商品数据，前端支持 SSE 流式与下单人工确认。

## 它解决什么

海淘/比价场景里，用户要的回答不是一段话，而是：符合约束的商品清单、多平台价格对比、差评与店铺风险、以及「降到位再提醒我」的持续跟踪。单轮问答做不完，所以拆成 Agent 编排：每个节点只做一件事，失败可降级、可重试、可断点续跑，最后把结论落成订单或订阅。

## 架构

```
前端  React + Vite + TS  ·  SSE 流式  ·  HITL 审批弹窗  ·  盯价/画像页面
  │
API   FastAPI（21 个接口）  ·  认证限流  ·  correlation_id 全链路追踪
  │
编排  LangGraph StateGraph  ·  Supervisor 动态路由  ·  PostgreSQL Checkpointer
  │
节点  搜货 → 比价 → 风险 → 下单确认（interrupt）→ 归档 → 盯价
  │
记忆  Redis 工作记忆  ·  PostgreSQL 情景记忆  ·  ChromaDB 长期画像（Token Budget 4,000）
  │
工具  MCP：商品搜索 / 价格查询 / 评论风险 / 商品详情（京东联盟真实 SKU + mock 兜底）
  │
基础  PostgreSQL · Redis · ChromaDB · Docker Compose · nginx HTTPS
```

节点间的容错设计：三态熔断器（CLOSED / OPEN / HALF_OPEN）+ 指数退避包住每次工具调用，重试耗尽写死信表并返回降级结果，图本身不中断。

## 快速开始

```bash
cp .env.example .env                 # 至少填 LLM_API_KEY；不填走关键词兜底
docker compose -p shopping-agent up -d postgres redis chroma
cd backend && python run.py          # 8000
cd mcp_server && python run.py       # 9000，不配京东联盟凭证时用 data/products.json 的 mock 数据
cd frontend && npm install && npm run dev
```

生产部署用 `docker-compose.prod.yml`（nginx 反代 + HTTPS），证书放 `certs/`，该目录已被 `.gitignore` 排除。

## 测试

```bash
cd backend   && python -m pytest tests/ -q   # 198 条
cd mcp_server && python -m pytest -q         # 32 条
cd frontend  && npm test                     # 55 条
```

后端行覆盖率 89%。测试对网络调用做了 mock，不需要真实京东联盟凭证。

## 评测

自建 200 条标注用例，用真模型跑三档工具故障注入（10% / 30% / 50%）：

| 指标 | 结果 |
|---|---|
| 意图路由准确率 | 94.5% |
| 故障恢复率（10% / 30% / 50% 故障） | 98% / 100% / 98%（验收线 90%） |

## 目录

```
backend/     FastAPI + LangGraph（app/graph 节点、app/memory 三层记忆、app/resilience 容错、
             app/evaluation 评测、app/api 接口）
mcp_server/  MCP 工具服务：sources 数据源适配（京东联盟 / mock）
frontend/    React + Vite 多页面 SPA
nginx/       生产反代配置
docs/        Prompt 优化需求、Checkpoint 迁移方案、前端改造方案
```

## 已知限制（如实写在这）

- 京东联盟账号权限只覆盖京粉精选池（约 6,800+ SKU），池外品类搜不到真实商品，会回落 mock 并标注来源，不是「全平台比价」。
- 自签证书，仅用于本地/内网演示，没有公网域名。
- LLM 依赖外部 OpenAI 兼容接口，限流时会退化到关键词路由，此时多轮理解能力明显下降。
- 京粉频道池首次探测需 3.5~5 分钟，所以挂了 `mcpcache` 命名卷做 6h 缓存。

## 设计文档

- [`architecture.md`](architecture.md)：分层、State Schema、节点职责与降级策略
- [`docs/checkpoint迁移方案.md`](docs/checkpoint迁移方案.md)：为什么从同步 Saver 换到 AsyncPostgresSaver
- [`docs/prompt优化需求.md`](docs/prompt优化需求.md)：Prompt 结构约束与验收用例
- [`搭建记录与不足.md`](搭建记录与不足.md)：逐轮开发日志，含踩坑与遗留问题

## License

MIT
