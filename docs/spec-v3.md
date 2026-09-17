# spec-v3：可解释增强

> 原则：只做数据之上的视图，不建系统。agent/proxy 核心行为不变。

## 目标

- **任一步骤可回放**：UI 步进审查（时间轴看每步推理/命令/流量）+ 请求重放（从 proxy.jsonl 重发并 diff）。
- **全量数据可导出标准格式**：messages JSONL / SARIF / HTML 报告，各自独立下载，不打包 zip。

## 数据基础

- `sessions/<id>/messages.jsonl`（新增）：run 结束后由 loop 返回的完整 messages 数组落盘（system/user/assistant/tool），作为训练数据与步进审查的数据源。loop 返回值扩为 4 元组 `(verdict, events, stopped_reason, messages)`。
- 其余（meta/events/proxy/report）不变。

## 回放

### 步进审查（前端）

会话详情页时间轴视图：每步 = 一个 StepEvent，展示 tag、cmd、result（截断可展开），以及该步时间窗内关联的 flow 事件（按 request_id 点开看 req/resp body）。数据源：`GET /api/sessions/{id}/events.jsonl`（读文件，非 SSE）。

### 请求重放

- `POST /api/sessions/{id}/replay`，body：`{"request_id": int, "overrides": {"method"?, "url"?, "body"?}}`（overrides 可选）。
- server 从 proxy.jsonl 取原始记录，用 httpx 重发（不经过审计代理，也不写 proxy.jsonl），返回 `{original: {...}, replayed: {...}, diff: {status_changed, body_changed}}`。
- 仅允许该会话 meta 中登记的 host:port（防止变成任意 SSRF 跳板）；blocked 记录拒绝重放。

## 导出

`GET /api/sessions/{id}/export?format=messages|sarif|html`，各自独立单文件下载：

- **messages**：`<id>.jsonl`，每行一个 message 对象（OpenAI 格式）。
- **sarif**：`<id>.sarif`，SARIF 2.1.0；每条 finding → result，ruleId=漏洞类型/标题，message=rationale + evidence request_id 列表，severity 映射 level（high→error, medium→warning, low→note）。
- **html**：`<id>.html`，自包含文字版报告（meta/stats/findings/discarded/证据 request_id 索引表），无外部资源。

## 文件落点

- `server/sessions.py`：loop 调用处适配 4 元组 + messages 落盘。
- `server/exporters.py`（新）：三种导出的纯函数。
- `server/app.py`：新端点（events 文件读、replay、export）。
- `web/`：时间轴视图、重放按钮/diff 展示、导出按钮组。
- `agent/loop.py` / `agent/loop_v2.py`：返回 messages。

## 测试

- 单测（零网络）：messages 落盘与导出、SARIF 映射（severity/level、evidence）、HTML 含关键段落、replay 的 override 合并 + 目标白名单校验（FakeLLM/Fake httpx transport）。
- e2e（`-m e2e`）：真实会话后导出三格式 + 重放一条 DVWA 请求。
