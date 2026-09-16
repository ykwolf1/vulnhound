"""Agent sandbox: run a docker container and exec commands inside it."""

import asyncio
import uuid
from dataclasses import dataclass

__all__ = ["ExecResult", "Sandbox", "SandboxNotAvailableError"]


class SandboxNotAvailableError(Exception):
    """Docker 不可用或镜像缺失，沙箱无法启动。"""


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    returncode: int


class Sandbox:
    def __init__(self, proxy_port: int, image: str = "vh-agent:latest"):
        self.proxy_port = proxy_port
        self.image = image
        self.container: str | None = None

    async def start(self) -> None:
        name = f"vh-sandbox-{uuid.uuid4().hex[:8]}"
        proxy = f"http://host.docker.internal:{self.proxy_port}"
        # curl 对 http:// URL 忽略大写 HTTP_PROXY，必须同时注入小写（经典坑）
        proxy_env = {
            "HTTP_PROXY": proxy,
            "http_proxy": proxy,
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "ALL_PROXY": proxy,
            "NO_PROXY": "localhost,127.0.0.1",
        }
        env_args: list[str] = []
        for k, v in proxy_env.items():
            env_args += ["-e", f"{k}={v}"]
        run = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            *env_args,
            self.image,
            "sleep",
            "infinity",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await run.communicate()
        if run.returncode != 0:
            raise SandboxNotAvailableError(
                f"docker run failed (rc={run.returncode}); image={self.image!r} missing or docker unavailable?"
            )
        self.container = name
        inspect = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await inspect.communicate()
        if inspect.returncode != 0 or out.decode().strip() != "true":
            raise SandboxNotAvailableError(
                f"container {name} not running after start: {err.decode().strip()}"
            )

    async def exec(self, cmd: str, timeout: int = 60) -> ExecResult:
        assert self.container, "Sandbox not started"
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self.container,
            "sh",
            "-c",
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(stdout="", stderr="timeout", returncode=-1)
        return ExecResult(stdout=stdout.decode(), stderr=stderr.decode(), returncode=proc.returncode)

    async def stop(self) -> None:
        if not self.container:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stop",
            self.container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()  # --rm 自动清理；幂等
        self.container = None
