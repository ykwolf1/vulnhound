# vulnhound · 产品设计规格（Spec）

**版本**：v0.1（MVP 定稿）
**日期**：2026-09-15
**状态**：待评审
**一句话**：给一个地址，AI 自己登录、自己找漏洞、给出证据可溯源的结构化报告。

---

## 1. 产品定位

AI 安全测试员：不是扫描器、不是平台——是一个**在沙箱里自由使用真实工具的 AI Agent**，其全部网络流量经审计代理记录与拦截，产出每个结论都能追溯到原始请求的报告。

三条设计红线（来自推倒重来的教训）：

1. **主线只有一条**：输入地址 → 看 AI 干活 → 拿报告。任何偏离此主线功能一律不做。
2. **平台保持薄**：单页、无登录、无管理后台。数据库可以没有，文件即状态。
3. **审计是物理结构不是功能**：AI 的自由在"怎么测"，证据在"流量记录 + 结构化结论"——两者都不在 LLM 手里。

## 2. 版本路线

| 版本 | 内容 | 出口标准 |
| --- | --- | --- |
| **MVP** | LLM 单轮驱动：登录目标 → 探测 → 判断 → 结构化报告 | 对 DVWA 完成一次端到端：AI 自主登录、发现 SQL 注入、报告含 request_id 证据引用 |
| **V2** | 强自主：观察→假设→验证→修正循环，多轮多方向；预算与无进展控制 | AI 在一次会话中自主切换 ≥2 个攻击方向并收敛 |
| **V3** | 可解释增强：每步可审查/可导出（训练数据/skill 进化燃料）。**只做数据之上的视图，不建系统** | 任一步骤可回放；全量数据可导出标准格式 |
| **V4** | 评测：性能（并发）与效果（证据质量/查全查准，基于已知漏洞对照） | 出一份量化评测报告 |
| **V5** | 企业级/多用户/可商用 | 另立规格 |

## 3. MVP 范围

### 3.1 必须有

- **单页 Web**（三态：输入 / 实时会话 / 报告；形态以 `prototype/index.html` 为交互基准）
- **AI Agent**：LLM（DeepSeek）单轮驱动；系统提示词一页纸（目标、凭证、输出 JSON schema、基本纪律："现象≠漏洞"、每条结论必须引用 request_id）；步数封顶 20 / 时长 30 分钟
- **沙箱**：Agent 在 Docker 容器内执行（真 shell：curl/python 等），容器网络出口仅指向审计代理
- **审计代理**（宿主机）：记录全部请求/响应（JSONL，每条带单调递增 `request_id`）；**三元组白名单**（host+port+scheme 完全匹配目标才放行，其余拦截并记录）
- **无进展检测**：连续 3 步响应高度相似或重复相同命令 → 终止并标记"无进展"
- **结构化报告**：JSON（发现：标题/严重度/判断依据/证据 request_id 引用）+ 页面渲染；被丢弃的信号（现象≠结果）单独列出
- **实时事件流**：AI 每步（思考/命令/结果）与代理流量实时推到页面

### 3.2 明确不做（MVP）

HTTPS MITM 解密（DVWA 是 HTTP；V2 用 mitmproxy 支持）、多用户、并发会话、持久数据库、登录鉴权、通知、报告导出多格式、漏洞类型枚举/方向策略（AI 自主决定测什么）。

## 4. 架构

```text
单页 Web（静态）
   │ SSE
薄后端 FastAPI（3 接口：POST /api/sessions、GET /events、GET /report）
   │ 每会话：起审计代理(随机端口) → 起 Agent 容器(--network 指向代理)
AI Agent 容器（LLM 循环 + 真 shell；HTTP_PROXY 注入）
   │ 全部流量
审计代理（记录 request_id×全量 → evidence/*.jsonl；三元组拦截）
   ▼
目标（DVWA @ http://127.0.0.1:8080）
```

## 5. 组件与仓库结构

```text
vulnhound/
├── prototype/   # 已有：三态原型（交互基准）
├── web/         # 单页（由原型演化）
├── server/      # FastAPI：会话生命周期、SSE、报告生成
├── agent/       # LLM 循环：提示词、步进控制、无进展检测、结论 schema 校验
├── proxy/       # 审计代理：流量记录 + 三元组白名单
└── docs/        # 本 spec
```

## 6. 关键契约

- **request_id**：代理分配，单调递增，结论 JSON 的 `evidence` 字段必须引用存在的 id（校验失败→纠错重试 1 次）
- **结论 schema**：`{findings: [{title, severity(high|medium|low), rationale, evidence: [request_id]}], discarded: [{title, reason}]}`（rationale ≤300 字）
- **SSE 事件**：`step`（tag: observe|act|hypo|verify + 内容）、`flow`（request_id/method/path/status/blocked）、`done`（报告就绪）
- **失败也是报告**：触顶/无进展/目标不可达 → 报告如实标注中止原因，已完成流量保留

## 7. 测试策略

- 单测（无 LLM/网络）：三元组拦截矩阵、request_id 分配、无进展检测、schema 校验、报告生成
- 集成（真 DVWA + 真 DeepSeek）：端到端一场 = MVP 出口
- Day-0 Spike（实施计划第一个任务）：DeepSeek 驱动协议选型——原生 tool calling vs 纯文本命令块解析，20 步小循环真打 DVWA 登录页，拿数据定协议

## 8. 技术选型

Python 3.12 / FastAPI / Docker（Agent 容器：python:3.12-slim + curl）/ DeepSeek（沿用现有 key）/ 前端由原型演化（原生 HTML/CSS + SSE，无需框架）。
