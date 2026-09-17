# vulnhound

输入一个地址，AI 在容器沙箱里自由测试，全部流量经审计代理留痕，产出证据可溯源的结构化漏洞报告。

## 架构

```
                    ┌────────────────────────── 宿主机 ──────────────────────────┐
                    │                                                            │
 浏览器 ──HTTP/SSE──▶ ⑤ server (FastAPI) ──驱动──▶ ③ agent loop (DeepSeek LLM)   │
                    │       sessions/<id>/            │ docker exec               │
                    │                                 ▼                           │
                    │                          ④ Agent 容器 (vh-agent)            │
                    │                                 │ HTTP_PROXY                 │
                    │                                 ▼                           │
                    │                 ① 审计代理 (三元组白名单, JSONL 留痕) ──转发──▶ ② 靶场 (DVWA)
                    └────────────────────────────────────────────────────────────┘
```

- ① `proxy/` 审计代理：host+port+scheme 完全匹配才放行，全量记录到 `proxy.jsonl`
- ② 靶场：任意 HTTP 目标（MVP 验证用 DVWA）
- ③ `agent/` LLM 循环：bash 命令协议驱动，Verdict 含 findings + evidence(request_id)
- ④ Agent 容器：命令执行沙箱，出口仅指向审计代理
- ⑤ `server/` 薄后端 + `web/` 原生前端：SSE 实时步进/流量，报告页

## 快速开始

最快路径（自动建 venv、装依赖、构建 `vh-agent` 镜像并起服务）：

```sh
export LLM_API_KEY=sk-...
./start.sh          # 默认 8900 端口，可用 PORT= 覆盖
```

手动步骤：

1. 起 DVWA 靶场（首次进入 `http://127.0.0.1:8080/setup.php` 完成 setup）：

   ```sh
   docker compose -f <旧仓库路径>/deploy/targets/dvwa.yml up -d
   ```

2. 安装：

   ```sh
   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
   ```

3. 构建 agent 镜像：

   ```sh
   docker build -t vh-agent agent/
   ```

4. 配置 LLM key（DeepSeek，OpenAI 兼容）：

   ```sh
   export LLM_API_KEY=sk-...
   ```

5. 起服务并打开前端：

   ```sh
   .venv/bin/uvicorn server.app:app --port 8900
   # 浏览器访问 http://127.0.0.1:8900
   ```

## 测试

```sh
.venv/bin/pytest -q          # 默认套件（FakeLLM，零网络零容器）
.venv/bin/pytest -q -m e2e   # 真实 e2e：需 DVWA + docker 镜像 + LLM_API_KEY（成本与时长自担）
```

## 目录结构

```
agent/    LLM 客户端、循环、沙箱执行器、Dockerfile
proxy/    审计代理（三元组白名单 + JSONL 留痕）
server/   FastAPI 后端与会话目录管理
web/      原生前端（SSE 实时流 + 报告页）
tests/    单测（默认）+ e2e（-m e2e）
docs/     spec 与实施计划
sessions/ 会话目录：meta.json / events.jsonl / proxy.jsonl / report.json
```

## V2：双层循环

V2 在原单层 ReAct 之上增加方向管理层（外层）：模型可通过 `switch_direction` 工具在多个测试方向间切换，每个方向内部仍是假设驱动的 observe→act→verify 循环。创建会话时用 `loop_version` 参数选择（`"v1"` 单层 / `"v2"` 双层，默认 v2）。预算：单方向 15 步（耗尽提醒切换）、总步数 100 步、总时长 20 分钟，触顶强制出报告（`stopped_reason=step_cap` / `time_cap`）。

## 已知边界（MVP）

- 会话为内存态实例缓存：服务重启后旧会话的报告可读，但事件流/SSE 不可恢复
- HTTPS MITM 不支持（CONNECT 一律 403），V2 再做
- 步数预算 40 步，触顶强制出报告（stopped_reason=step_cap）
