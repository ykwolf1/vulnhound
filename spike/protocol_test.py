# throwaway spike — Day-0 协议选型试验脚本，仅用于生成 docs/spike-decision.md 的数据，不进入产品代码。
"""比较两种 DeepSeek 驱动协议在 20 步 agent 循环上的表现（真 DeepSeek + 真 DVWA，HTTP 直连）。

协议 A：OpenAI 原生 tool calling（tools=[execute_command]），收到 tool_calls 后本地 shell 执行并回喂。
协议 B：纯文本协议 —— 系统提示词要求每轮输出一个 ```bash 围栏，解析后执行回喂；输出 DONE 结束。

任务：登录 DVWA（admin/password，需先取 user_token）并确认到达 index.php（响应含 Welcome）。
用法：spike/protocol_test.py <A|B|ALL>   （默认 ALL，各跑 3 次）
"""
import json
import os
import re
import subprocess
import sys
import time

import httpx

API_KEY = os.environ.get("LLM_API_KEY") or re.search(
    r"^LLM_API_KEY=(.+)$",
    open("/Users/yangkun/Desktop/Projects/Smart_Vulnerability_Hunting/backend/.env").read(),
    re.M,
).group(1).strip()
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"
DVWA = "http://127.0.0.1:8081"
MAX_STEPS = 20

TASK = (
    f"任务：登录 DVWA（地址 {DVWA}，账号 admin / 密码 password）。"
    "DVWA 登录需要 CSRF user_token：先 GET login.php 从 HTML 中提取 name='user_token' value='...'，"
    "再带 cookie POST username/password/user_token 到 login.php。"
    "登录成功后 GET index.php 确认响应包含 Welcome。确认后立即结束。"
)

SYSTEM_COMMON = (
    "你是一个命令行驱动 agent，与一台已能访问目标的机器交互，机器上有 curl。"
    "每一步只做一件事。用 curl 时加 -s -L，需要保存/复用 cookie 时用 -c/-b 加同一个 cookie 文件（如 /tmp/dvwa.spike）。"
    "命令输出若太长请自行用 grep/head 截取关键部分再执行。"
)

SYSTEM_B = (
    SYSTEM_COMMON
    + "\n输出格式（严格遵守）：每轮只输出一个 ```bash 围栏代码块，内含一条要执行的 shell 命令，"
    "围栏外可以有简短说明。我会执行命令并把输出原样回给你。"
    "达成任务目标后，不再输出围栏，只输出一行 DONE。"
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "execute_command",
        "description": "在 shell 执行命令",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
}]


def chat(client, payload):
    r = client.post(f"{BASE_URL}/v1/chat/completions", json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def run_cmd(cmd):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        out = "[TIMEOUT 60s]"
    out = out.strip()
    if len(out) > 4000:
        out = out[:2000] + "\n...[truncated]...\n" + out[-1500:]
    return out or "[no output]"


def check_success():
    """独立验证：是否真的登录并见到 Welcome。"""
    subprocess.run("rm -f /tmp/dvwa.verify", shell=True, capture_output=True)
    out = run_cmd(
        f"curl -s -c /tmp/dvwa.verify {DVWA}/login.php | grep -oE \"user_token' value='[a-f0-9]+\" | grep -oE '[a-f0-9]+$'"
    )
    if not re.fullmatch(r"[a-f0-9]+", out or ""):
        return False
    run_cmd(f"curl -s -b /tmp/dvwa.verify -c /tmp/dvwa.verify -d 'username=admin&password=password&Login=Login&user_token={out}' {DVWA}/login.php -o /dev/null")
    page = run_cmd(f"curl -s -b /tmp/dvwa.verify -L {DVWA}/index.php")
    return "Welcome" in page


def run_protocol_a(run_id):
    messages = [
        {"role": "system", "content": SYSTEM_COMMON},
        {"role": "user", "content": TASK + " 通过调用 execute_command 工具执行命令；完成后不再调用工具，直接回复文本总结。"},
    ]
    steps = parse_fail = 0
    done = False
    for step in range(1, MAX_STEPS + 1):
        steps = step
        msg = chat(client, {"model": MODEL, "messages": messages, "tools": TOOLS})
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            done = True
            break
        for call in calls:
            fn = call["function"]
            try:
                cmd = json.loads(fn["arguments"])["cmd"]
            except Exception:
                parse_fail += 1
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": "ERROR: 参数 JSON 解析失败，请重新调用 execute_command 并给出合法 JSON。"})
                continue
            out = run_cmd(cmd)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
    ok = check_success()
    json.dump(messages, open(f"/tmp/spike-A-{run_id}.json", "w"), indent=1, ensure_ascii=False)
    return {"run": run_id, "success": ok, "steps": steps, "parse_fail": parse_fail, "stuck": not done and not ok}


def extract_bash_fence(text):
    m = re.search(r"```(?:bash|sh|shell)?\s*\n(.*?)```", text, re.S)
    return m.group(1).strip() if m else None


def run_protocol_b(run_id):
    messages = [
        {"role": "system", "content": SYSTEM_B},
        {"role": "user", "content": TASK},
    ]
    steps = parse_fail = 0
    done = False
    for step in range(1, MAX_STEPS + 1):
        steps = step
        msg = chat(client, {"model": MODEL, "messages": messages})
        content = msg.get("content") or ""
        messages.append({"role": "assistant", "content": content})
        if re.search(r"^\s*DONE\b", content.strip(), re.M) or content.strip() == "DONE":
            done = True
            break
        cmd = extract_bash_fence(content)
        if not cmd:
            parse_fail += 1
            messages.append({"role": "user", "content": "ERROR: 未找到 ```bash 命令块。请严格按格式输出一个 ```bash 围栏。"})
            continue
        out = run_cmd(cmd)
        messages.append({"role": "user", "content": f"命令输出：\n{out}"})
    ok = check_success()
    json.dump(messages, open(f"/tmp/spike-B-{run_id}.json", "w"), indent=1, ensure_ascii=False)
    return {"run": run_id, "success": ok, "steps": steps, "parse_fail": parse_fail, "stuck": not done and not ok}


if __name__ == "__main__":
    which = sys.argv[1].upper() if len(sys.argv) > 1 else "ALL"
    client = httpx.Client(headers={"Authorization": f"Bearer {API_KEY}"})
    results = {}
    if which in ("A", "ALL"):
        results["A"] = []
        for i in range(1, 4):
            t0 = time.time()
            r = run_protocol_a(i)
            r["secs"] = round(time.time() - t0, 1)
            results["A"].append(r)
            print("A", r, flush=True)
    if which in ("B", "ALL"):
        results["B"] = []
        for i in range(1, 4):
            t0 = time.time()
            r = run_protocol_b(i)
            r["secs"] = round(time.time() - t0, 1)
            results["B"].append(r)
            print("B", r, flush=True)
    print(json.dumps(results, indent=2))
