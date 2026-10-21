# Prompt 优化需求（AI 编程助手执行版）

> 本文档是重写 `backend/app/prompts/` 两份 prompt 的执行依据：AI 编程助手直接按此产出（2026-10-12 分工调整，GPT 复核可选）。
> 改完后直接覆盖原文件（保持 `.md` 格式和 `## 系统提示词` / `## 输出格式约定` 两个小节）。
> 代码侧零改动——所有约束都是为了对齐现有解析逻辑。

---

## 0. 系统背景

电商消费决策 Agent，主流程是一张 LangGraph 图：

```
supervisor → search → compare → risk → (prepare_order → approval) → archive → end
                                                          ↘ monitor → end
```

- **supervisor**：解析用户意图，决定走哪个分支。
- **search/compare/risk**：调 MCP 工具（商品搜索、比价、风险评估）。
- **order_requested=true** 时进 `prepare_order → approval`（人工审批下单）。
- **monitor** 分支：盯价，`target_price` 字段当降价阈值用（2026-10-17 前错用 `budget`）。

两份 prompt 都走同一个 LLM 客户端（`app/llm.py`），OpenAI 兼容接口，`json_mode=True`（`response_format: json_object`），模型默认 `gpt-4o-mini`。

---

## 1. 共用约束（两份都要遵守）

### 1.1 文件结构（不能动）

每个 `.md` 文件必须包含且只包含这两个 `##` 小节，顺序固定：

```
## 系统提示词
...（给 LLM 的正文，主要改这里）...

## 输出格式约定（代码依赖，勿改）
...（代码按这里解析，不要改这一节的文字）...
```

代码用 `load_prompt(filename, "系统提示词")` 只抽取 `## 系统提示词` 小节正文发给 LLM。
`## 输出格式约定` 是给人看的契约说明，不进 prompt 正文，但必须严格按它设计输出。

### 1.2 输出硬约束

- LLM 开了 `json_mode`，输出必须是**合法 JSON 对象**，不能有 markdown 围栏、解释文字、前后缀。
- 代码侧 `parse_json` 虽然能剥围栏，但开了 `json_object` 后模型被强制输出 JSON，最好不要依赖剥围栏。
- 字段缺失或类型错，代码会 `get()` + 默认值兜底，不会崩，但会丢信息。所以要引导模型字段给全。

---

## 2. Prompt A：Supervisor 意图解析

**文件**：`backend/app/prompts/supervisor_intent.md`
**调用方**：`app/graph/nodes/supervisor.py` → `_llm_route()`

### 2.1 当前草稿（供参考，重写系统提示词正文时以此为基）

```
你是电商消费决策系统的意图路由器。判断用户这句话属于哪类需求，只输出一个 JSON 对象，不要输出其他任何内容：

{"intent": "...", "category": "", "budget": null, "constraints": [], "order_requested": false}

intent 只能是以下六个之一：
- search：想找商品、求推荐、看看有什么
- compare：想比价、问哪个平台便宜
- risk_check：想知道店铺/商品靠不靠谱、有没有坑
- archive：想保存记录、管理购物档案
- monitor：想盯价、降价了提醒我
- unknown：都不是，或者拿不准

规则：
- category：用户提到的商品品类（如"降噪耳机"），没提就填 null
- budget：把预算抽成纯数字，"2000块以内" → 2000，没提就 null
- constraints：用户提的硬性要求列表（品牌、颜色、尺寸等），没提就空数组
- order_requested：用户明确表达"下单/购买/买下来"才为 true，只是浏览不是
- 上下文里之前聊过的内容可以参考，但只判断本次输入的意图
```

### 2.2 代码消费逻辑（重写时必须对齐）

1. 系统提示词 + 用户内容（含最近 3 条历史对话）一起发给 LLM，`json_mode=True`。
2. `parse_json` 解析后，校验 `intent` 必须在 `{"search","compare","risk_check","archive","monitor","unknown"}` 里。
   - **非法 intent → 丢弃，走关键词兜底**。所以 intent 必须严格六选一，不能出现拼写变体。
3. 解析结果写进 `state.query`：

   | 字段 | 类型 | 下游怎么用 |
   |---|---|---|
   | `intent` | string（六选一） | 决定图的分支走向 |
   | `category` | string \| null | `search.py` 当搜索关键词；为 null 时退化为用 `raw`（用户原话）搜 |
   | `budget` | number \| null | `search.py` 透传给 MCP 做价格过滤（「我最多花多少」） |
   | `target_price` | number \| null | `monitor.py` 当盯价降价阈值（「降到多少通知我」），与 budget 无关 |
   | `drop_percent` | number \| null | 按百分比说的降幅（「降价 10% 提醒我」→ 10），与 target_price 二选一；调度侧按基准价折算触发线 |
   | `constraints` | string[] | `search.py` 透传给 MCP（品牌、颜色等硬约束） |
   | `order_requested` | bool | `builder.py` 为 true 才进 `prepare_order → approval` 审批下单 |

4. `next_node` 直接等于 `intent`，所以 `intent` 的值就是节点名。`unknown` 会走到 end（空跑）。

### 2.3 优化目标

1. **意图边界要清晰**：尤其是 search vs compare vs risk_check 的边界。用户说「这个耳机靠谱吗」→ risk_check；「哪个平台便宜」→ compare；「推荐个耳机」→ search。给 2-3 组易混 case 的判断示例。
2. **category 提取要准**：从自然语言里抽品类短语。「我想买个平时通勤用的降噪耳机」→ category="降噪耳机"，不要把「通勤」塞进去。
3. **budget 解析要鲁棒**：「2000 左右」「两千以内」「预算 2k」「不超过 1500」都要抽出数字。**中文数字（两千、三百）必须转成阿拉伯数字**。无法解析就 null，不要瞎猜。
4. **constraints 要自包含**：每条是独立的硬约束短语，如 `["品牌:索尼", "颜色:黑色", "无线"]`，不要拆成碎词。
   > 2026-10-17 修订（第二十九轮）：**constraints 只装本次输入里明说的**。
   > 原来的写法允许「画像说我只买索尼，本次没提品牌就补进 constraints」，真模型照做，
   > 把长期画像里的品牌（纽曼）当硬约束，「帮我找个耳机」从 30 件缩成 3 件——用户
   > 根本没提过这个牌子。现在「已知用户背景」（画像 + 会话摘要 + 历史对话）只能用来
   > 补全指代、统一推荐口径，**一律不得进 constraints**；补全指代也只补到品类为止，
   > 不许把上文里的品牌/型号顺手捞进来当条件。
   > 代码侧另加了一道兜底 `supervisor._drop_unsaid_brands`：`品牌:x` 里的 x 在本轮
   > 用户输入里找不到字面就丢掉（品牌是专有名词，真说了必然能对上）。
5. **order_requested 要保守**：只有明确说「买」「下单」「帮我买」才 true。「看看」「推荐」「了解一下」都是 false。这是审批下单的开关，误判会打扰用户。
6. **unknown 的使用**：闲聊（「你好」「今天天气怎么样」）、无法归类的需求才 unknown。不要把模糊的购物需求归 unknown——归 search 比 unknown 好（search 至少会搜一下）。

### 2.4 验收用例（10 条，重写后跑）

| # | 用户输入 | 期望 intent | 期望 category | 期望 budget | 期望 order_requested |
|---|---|---|---|---|---|
| 1 | 帮我找个耳机 | search | 耳机 | null | false |
| 2 | 推荐个 2000 以内的降噪耳机 | search | 降噪耳机 | 2000 | false |
| 3 | 小米 buds 4 pro 哪个平台最便宜 | compare | 小米 buds 4 pro | null | false |
| 4 | 索尼 wh1000xm5 靠谱吗，有没有坑 | risk_check | 索尼 wh1000xm5 | null | false |
| 5 | 这个耳机帮我盯一下，降价了叫我 | monitor | 耳机 | null | false |
| 6 | 帮我下单买这个 | search* | null | null | true |
| 7 | 把刚才看的耳机存一下 | archive | null | null | false |
| 8 | 你好 | unknown | null | null | false |
| 9 | 预算一千五，要黑色的头戴式耳机，只要索尼 | search | 头戴式耳机 | 1500 | false |
| 10 | airpods pro 2 多少钱，比一比 | compare | airpods pro 2 | null | false |

\* 第 6 条 intent 可以是 search 或 compare，只要 `order_requested=true` 即可（下单前总要先有商品）。

\*\* 第二十九轮另加一组「带用户背景」的验收（6 条，真模型跑）：把画像写成
「偏好品牌：纽曼 / 只买索尼的耳机」、摘要里提纽曼，再跑上面第 1、5 条这类没提品牌的输入，
`constraints` 必须仍是 `[]`；只有本次明说品牌（「索尼的降噪耳机」「小米或者华为的耳机」）
才允许出现 `品牌:…`。验收结果 6/6。

### 2.5 不能改的

- `## 输出格式约定` 小节的文字。
- JSON 的字段名和结构（`intent / category / budget / target_price / drop_percent / constraints / order_requested`）。
  > 2026-10-17 修订：新增 `target_price`。原先 `budget` 一个字段既当「我最多花多少」
  > 又当「降到多少提醒我」，用户说「预算 500」就被当成「降到 500 提醒我」，
  > 一注册就对着现价远低于 500 的商品推一堆「降价提醒」——一件都没降。
  > 2026-10-20 修订：新增 `drop_percent`。用户说「降价 10% 提醒我」是相对盯价那刻的价
  > 跌 10%，原先只认 `target_price`，这种订阅阈值落成 null，`check_prices` 直接跳过、
  > 永远不提醒。现在百分比进 `drop_percent`（与 `target_price` 二选一），
  > 调度侧按基准价折算触发线。
- 六个 intent 的枚举值和含义。

---

## 3. Prompt B：会话摘要

**文件**：`backend/app/prompts/session_summary.md`
**调用方**：`app/api/chat.py` → `_make_summary()`

### 3.1 当前草稿

```
你是购物助理的记忆模块。会话结束了，把这次对话浓缩成结构化摘要，只输出一个 JSON 对象：

{"topics": [], "preferences": [], "brands": [], "budget_habits": "", "summary": ""}

字段含义：
- topics：这次聊了什么品类/事情，短语列表
- preferences：用户暴露的稳定偏好（"只买索尼"、"不买二手"、"要快的"），一条一个，供长期画像使用；没有就空数组
- brands：提到的品牌列表
- budget_habits：预算习惯的一句话描述，没有就空字符串
- summary：两三句话说清这次会话干了什么、结论是什么

规则：
- 只记稳定、可复用的事实，一次性的闲聊不要记
- 拿不准的偏好不要编，宁缺毋滥
```

### 3.2 代码消费逻辑（重写时必须对齐）

1. 系统提示词 + 一段事实 JSON（`user_input / intent / products / cheapest / risk`）发给 LLM，`json_mode=True`。
2. `parse_json` 解析后整个 dict 存进 PostgreSQL `episodic_memory.summary` 字段。
3. **`preferences` 字段有特殊用途**：前 5 条会被逐条写进 ChromaDB 做长期画像向量检索。
   - 每条 preference 是一个独立文档，必须**自包含、能独立看懂**。
   - 坏例子：`"便宜的"`（脱离上下文不知道说什么）。
   - 好例子：`"买耳机优先选拼多多，价格最低"`。
4. 新会话开头会检索最近 5 条摘要，拼成自然语言塞进 `memory_context` 给 Supervisor 参考。

### 3.3 优化目标

1. **preferences 是重点**：每条必须是「主语 + 偏好」的完整陈述，能脱离上下文独立理解。宁缺毋滥，不确定的不要编。
2. **区分稳定偏好 vs 一次性需求**：「这次预算 2000」是一次性的，不要进 preferences；「只买索尼」「不买二手」「只看拼多多」是稳定偏好，要进。
3. **brands 去重**：同一会话提到多个品牌就都列，别重复。
4. **budget_habits**：如果用户反复提预算范围/消费习惯，总结成一句话；单次提预算不要写进来。
5. **summary 简洁**：两三句，说清干了啥、结论是啥（比如「用户想找降噪耳机，比价后小米 buds 4 pro 拼多多 799 最便宜，风险评估无异常」）。
6. **topics**：品类短语列表，如 `["降噪耳机", "头戴式耳机"]`。

### 3.4 不能改的

- `## 输出格式约定` 小节的文字。
- JSON 的字段名和结构（`topics / preferences / brands / budget_habits / summary`）。
- `preferences` 是字符串数组（每条是自包含短语）这个约定。

---

## 4. 交付方式

由 AI 编程助手直接重写 `backend/app/prompts/` 下两份文件（保持 `## 系统提示词` + `## 输出格式约定` 结构）。产出物可交 GPT 复核一遍，复核意见贴回搭建记录对应步骤下。

改完后做两步验证：
- Supervisor：跑上面 10 条用例，检查 intent / category / budget / order_requested 是否符合期望（需配 `LLM_API_KEY`）。
- 摘要：发一两轮真实对话，检查 preferences 是否自包含、summary 是否准确。
