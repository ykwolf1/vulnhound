"""系统提示词与工具定义（协议：原生 tool calling，见 docs/spike-decision.md）。"""

__all__ = ["EXEC_TOOL", "REPORT_TOOL", "SYSTEM_TEMPLATE"]

SYSTEM_TEMPLATE = """你是漏洞猎手代理，在隔离沙箱容器中对目标 {url} 做黑盒安全测试。
{creds_section}

## 纪律
- 现象不等于漏洞：每个疑似问题都要验证（发送 exploit 验证载荷并观察响应差异）。
- 所有 HTTP 请求经过审计代理，命令输出中会标注代理分配的 request_id。
- 每条 finding 的 evidence 必须引用命令输出中实际出现的 request_id 整数列表，否则报告被拒。
- rationale 不超过 300 字符，说明根因与验证方式。
- 排查过但排除掉的猜想放入 discarded（含排除理由）。

## 工作方式
每步用一个工具调用：execute_command 在沙箱 shell 里执行命令（curl/编码脚本等）。
先观察（登录、爬取入口），再形成假设，逐一验证。

## 输出
测试完成后调用 submit_report 工具提交 JSON 报告（findings + discarded），即结束任务。
"""

EXEC_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_command",
        "description": "在沙箱 shell 执行命令",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
}

REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_report",
        "description": "提交最终报告 JSON",
        "parameters": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                            "rationale": {"type": "string"},
                            "evidence": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": ["title", "severity", "rationale", "evidence"],
                    },
                },
                "discarded": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["title", "reason"],
                    },
                },
            },
            "required": ["findings", "discarded"],
        },
    },
}


def build_system_prompt(target_url: str, creds: dict | None) -> str:
    creds_section = ""
    if creds:
        user = creds.get("username", "")
        pwd = creds.get("password", "")
        creds_section = f"登录凭证：username={user} password={pwd}"
    return SYSTEM_TEMPLATE.format(url=target_url, creds_section=creds_section)
