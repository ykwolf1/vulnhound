# spec-v4：评测

> 目标：性能（并发）与效果（证据质量/查全查准，基于已知漏洞对照），产出量化评测报告。

## 形态

`eval/` 评测子系统 + CLI + Web 评测页。复用现有会话机制（server.sessions），不改 agent 核心。

## 数据与打分

### Ground truth

`eval/groundtruth/dvwa.json`：DVWA 已知漏洞清单，条目 = `{module: "/vulnerabilities/sqli/", vuln_type: "SQL Injection", severity}`。评测集可配置（`--gt` 选文件）。

### 打分（eval/scoring.py，纯函数）

findings → evidence 的 request_id → proxy.jsonl 的 URL 提取 DVWA 模块路径 → 与 GT 按模块匹配：

- **查准率 precision**：命中 GT 模块的发现 / 全部发现
- **查全率 recall**：被发现的 GT 模块数 / GT 总数
- **证据有效率**：evidence 的 request_id 存在于 proxy.jsonl 且 URL 指向该发现声称的模块的比例
- 误报（不命中 GT 的发现）与漏报（未发现的 GT）清单

### 跑批（eval/runner.py）

依次/并发发起 N 次会话（复用 `create_session` + `Session.run`），收集每次：耗时、步数、流量数、findings、打分结果。LLM/沙箱可注入 Fake 供单测。

### 并发性能（eval/perf.py）

K 路并发跑批，测总时长、单会话平均时长、成功率。MVP 只报数字，不做资源隔离。

### 报告（eval/report.py）

聚合为 JSON 落 `eval/results/<eval_id>.json` + 渲染 Markdown（`eval/results/<eval_id>.md`）。

## 入口

- **CLI**：`python -m eval --target http://127.0.0.1:8080 --runs 3 --concurrency 1 --gt dvwa`
- **Web**：`web/eval.html` 历史评测列表 + 单次详情（分数、误报/漏报、会话明细）；API：`GET /api/evals`、`GET /api/evals/{id}`、`POST /api/evals`（后台跑，参数 target/runs/concurrency）。

## 测试

- scoring：模块提取、匹配、precision/recall/证据有效率、误报漏报——纯函数单测。
- runner/perf：FakeLLM + Fake 沙箱跑通聚合逻辑。
- Web API：Fake 注入下列表/详情/发起。
- 真实评测：DVWA + key 自担成本，产出一份真实报告。
