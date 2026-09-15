"""Sandbox runner tests — monkeypatched subprocess, zero real docker."""

import asyncio

import pytest

from agent.runner import ExecResult, Sandbox, SandboxNotAvailableError


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout.encode()
        self.stderr = stderr.encode()
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


class Recorder:
    """Replace asyncio.create_subprocess_exec; records argv, returns queued procs."""

    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    async def __call__(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.procs.pop(0)


async def test_start_runs_detached_container_with_proxy_env(monkeypatch):
    rec = Recorder([FakeProc(stdout="abc12345\n", returncode=0), FakeProc(stdout="true\n", returncode=0)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
    sb = Sandbox(proxy_port=8899)
    await sb.start()
    run_argv = rec.calls[0][0]
    assert run_argv[0] == "docker"
    assert "-d" in run_argv and "--rm" in run_argv
    assert "-e" in run_argv
    envs = [run_argv[i + 1] for i, a in enumerate(run_argv) if a == "-e"]
    assert "HTTP_PROXY=http://host.docker.internal:8899" in envs
    assert "HTTPS_PROXY=http://host.docker.internal:8899" in envs
    assert run_argv[-2:] == ("vh-agent:latest", "sleep", "infinity")[-2:]
    assert "sleep" in run_argv and "infinity" in run_argv
    assert sb.container.startswith("vh-sandbox-")
    # inspect 确认运行态
    assert rec.calls[1][0][:3] == ("docker", "inspect", "-f")


async def test_exec_parses_result_fields(monkeypatch):
    rec = Recorder([FakeProc(stdout="hello\n", stderr="warn", returncode=3)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
    sb = Sandbox(proxy_port=8899)
    sb.container = "vh-sandbox-deadbeef"
    res = await sb.exec("curl http://x")
    assert isinstance(res, ExecResult)
    assert res == ExecResult(stdout="hello\n", stderr="warn", returncode=3)
    argv = rec.calls[0][0]
    assert argv[:3] == ("docker", "exec", "vh-sandbox-deadbeef")
    assert argv[-3:] == ("sh", "-c", "curl http://x")


async def test_exec_timeout_returns_minus_one(monkeypatch):
    class SlowProc(FakeProc):
        async def communicate(self):
            await asyncio.sleep(10)

    rec = Recorder([SlowProc()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
    sb = Sandbox(proxy_port=8899)
    sb.container = "vh-sandbox-deadbeef"
    res = await sb.exec("sleep 100", timeout=1)
    assert res.returncode == -1
    assert res.stderr == "timeout"


async def test_stop_calls_docker_stop(monkeypatch):
    rec = Recorder([FakeProc()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
    sb = Sandbox(proxy_port=8899)
    sb.container = "vh-sandbox-cafe0001"
    await sb.stop()
    assert rec.calls[0][0] == ("docker", "stop", "vh-sandbox-cafe0001")


async def test_stop_idempotent_when_no_container(monkeypatch):
    called = []

    async def noop(*a, **k):
        called.append(a)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", noop)
    sb = Sandbox(proxy_port=8899)
    await sb.stop()  # 未 start，不应抛错
    await sb.stop()
    assert called == []


async def test_start_inspect_failure_raises(monkeypatch):
    rec = Recorder([FakeProc(stdout="abc12345\n"), FakeProc(stderr="No such object", returncode=1)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
    sb = Sandbox(proxy_port=8899)
    with pytest.raises(SandboxNotAvailableError):
        await sb.start()
