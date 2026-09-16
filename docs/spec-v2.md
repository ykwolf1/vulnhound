# vulnhound V2 Spec · 强自主（双层循环）

**版本**：v0.1
**日期**：2026-09-16
**上游**：docs/spec.md §2 V2 定义（观察→假设→验证→修正循环，多轮多方向）
**MVP 实测依据**：真实会话 47 步/88 流量验证了单循环可行，但方向选择线性、预算不足、无强制切换

---

## 1. 目标

把 MVP 的单层 ReAct 循环升级为**双层循环**：外层方向管理（选择/切换/收敛）+ 内层假设驱动验证（观察→假设→命令→响应→修正）。

**出口标准**：一次会话中 AI 自主切换 ≥2 个攻击方向，至少 1 个方向走到"验证结论"（发现或排除均有据），总步数 100 内收敛提交报告。

## 2. 循环结构

```text
外层：DirectionManager（方向管理器）
├── 选择方向（从目标观察 + 已有发现推导，或开拓新方向）
├── 分配预算（每方向 ≤15 步）
├── 监控收敛（有结论 / 无进展 / 预算耗尽 → 切换或收卷）
└── 收卷（全部方向处理完或总预算尽 → 强制 submit_report）

内层：HypothesisLoop（假设驱动验证）
├── 观察（命令输出 / 响应内容）
├── 假设（"如果 X 成立，则 Y 应该发生"）
├── 验证命令（最小代价验证假设）
├── 响应分析（支持 / 反驳 / 无信息）
└── 修正（假设修正 / 换验证方式 / 报告方向结论）
```

## 3. 实现规格

### 3.1 数据模型

```python
class Direction(BaseModel):
    id: str                    # dir-001, dir-002…
    name: str                  # "sqli-id-param"
    hypothesis: str            # 当前假设
    status: str                # exploring | concluded | abandoned
    steps_used: int
    outcome: str | None        # 结论摘要（concluded 时填）

class DirectionEvent(StepEvent 的超集):
    tag 增加: "direction"      # 方向切换/新建/结论事件
    direction_id: str | None   # 关联方向
```

### 3.2 双工具扩展（三工具）

在 `execute_command` / `submit_report` 之上增加：

```python
SWITCH_DIRECTION_TOOL = {
    "name": "switch_direction",
    "description": "结束当前方向（可带结论），开启新方向或回归已有方向",
    "parameters": {
        "current_outcome": "str | None — 当前方向的结论摘要（有发现/排除/无进展）",
        "next_direction": "str — 新方向名（如 sqli-blind, xss-stored, auth-bypass）",
        "hypothesis": "str — 新方向的初始假设",
    },
}
```

### 3.3 循环逻辑

```python
async def run_agent_v2(llm, sandbox, target_url, creds,
                       max_steps=100,        # 总预算
                       max_steps_per_direction=15,  # 方向预算
                       ) -> tuple[Verdict, list[StepEvent], str]:
    directions: list[Direction] = []
    current: Direction | None = None

    while n < max_steps:
        # 内层：常规 ReAct（execute_command 为主）
        # 假设驱动由提示词模板强制：每步命令前须说明"当前假设"和"此命令验证什么"
        resp = await llm.complete(messages, tools=[EXEC, REPORT, SWITCH])

        if resp 调用 switch_direction:
            记录 current.outcome → directions.append(current)
            current = Direction(新方向)
            方向切换检查点：切换时注入速查卡（防遗忘）

        if resp 调用 submit_report:
            校验 + 返回（同 MVP）

        方向预算检查：current.steps_used >= 15 → 注入提醒"此方向预算尽，请 switch 或 submit"
        无进展检测：同 MVP（连续 3 步无新信息 → 强制 switch）

    # 外层收卷：强制 submit（同 MVP 的强制收卷轮）
```

### 3.4 提示词变更

- 系统提示词增加**方向管理段落**：初始方向推荐策略（登录态/技术栈/高 ROI 功能优先）、方向切换时机、每方向预算意识
- 每步输出格式增加两个字段：`hypothesis`（当前验证的假设）和 `purpose`（此命令验证什么）
- 方向切换时**注入速查卡**（防长对话遗忘纪律——来自外部设计指南的防遗忘机制）

### 3.5 前端

- 时间线按**方向分组**渲染（每个方向一个可折叠段，标题=方向名+结论徽章）
- 顶栏仪表增加：当前方向名 / 方向数 / 方向预算
- 报告的 findings 关联到产生它的方向（方向→发现的归属可见）

### 3.6 保留不变

审计代理（三元组/记录）、沙箱（容器/代理注入大小写）、报告 schema、SSE 事件流（新增 direction 事件类型，前端向后兼容处理未知 tag）。

## 4. 不做

方向间的并行（V2 单方向串行切换）；方向管理器独立 LLM 调用（同一个模型在同一次对话中管理方向，不拆二次调用）；自动方向发现（方向由模型自主命名，不预设枚举）。

## 5. 测试策略

- 单测（FakeLLM 脚本）：方向创建/切换/预算尽提醒/无进展强制切换/速查卡注入时机/强制收卷含方向元数据
- e2e（真 DVWA + 真 DeepSeek）：断言 directions ≥2、至少一个 concluded 且 outcome 非空、总步数 ≤100
