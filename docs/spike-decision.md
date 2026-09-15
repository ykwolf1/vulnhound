# Day-0 Spike 决策：Agent 驱动协议选型

日期：2026-09-15（Task 1，throwaway spike，脚本在 `spike/protocol_test.py`）

## 结论：采用协议 A —— OpenAI 原生 tool calling

## 实测数据

环境：真 DeepSeek（`deepseek-v4-flash`，OpenAI 兼容 API）+ 真 DVWA（HTTP 直连 127.0.0.1:8081，无代理/容器），任务 = 登录 DVWA（admin/password，先取 user_token）并确认 index.php 含 Welcome，上限 20 步，各 3 次。

| 协议 | Run | 成功 | 步数 | 解析失败 | 卡死 | 耗时(s) |
|---|---|---|---|---|---|---|
| A: tool calling | 1 | ✅ | 4 | 0 | 否 | 3.8 |
| A: tool calling | 2 | ✅ | 4 | 0 | 否 | 4.1 |
| A: tool calling | 3 | ✅ | 4 | 0 | 否 | 3.7 |
| B: ```bash 围栏 | 1 | ✅ | 4 | 0 | 否 | 4.0 |
| B: ```bash 围栏 | 2 | ✅ | 5 | 0 | 否 | 5.9 |
| B: ```bash 围栏 | 3 | ✅ | 4 | 0 | 否 | 4.1 |

汇总：A 3/3 成功（平均 4.0 步），B 3/3 成功（平均 4.3 步）；本次简单任务下两者均无解析失败、无卡死。

## 理由

- **稳定性**：本任务规模下两者都 0 解析失败，说明 deepseek-v4-flash 的围栏格式遵循度也不错。但 B 的正确性依赖提示词约束 + 正则提取，失败模式（无围栏、多个围栏、围栏外夹带、DONE 漏输出）在更长的渗透任务里概率会上升；A 的 `tool_calls.arguments` 由 API 结构化保证，天然免解析，失败面只剩 JSON 参数本身（本测 0 次）。
- **步数**：A 平均 4.0 步，B 平均 4.3 步（B 有一次多花一步），基本持平；A 还支持一轮多个 tool_calls，长任务有扩展空间。
- **实现复杂度**：两者实现量相当（A 处理 tool_calls 回喂，B 处理围栏正则 + DONE 判定 + 错误重提示）。但 B 需要自维护格式纠错分支（本次代码中已体现），A 的循环更少状态分支。
- **结论**：MVP 选 **协议 A（tool calling）**——同等表现下结构性风险更低、无自研文本协议需维护；B 留作模型不支持 function calling 时的降级备选。

## 备注（spike 过程发现）

- 宿主 8080 已被 Struts2 容器占用，DVWA 本次起在 127.0.0.1:8081（`deploy/targets/dvwa.yml` 端口后续需固定下来）。
- DVWA 首次使用需 POST setup.php 初始化数据库。
- 登录 POST 必须带 `Login=Login` 字段（缺它返回 200 重新渲染表单而非 302）。
