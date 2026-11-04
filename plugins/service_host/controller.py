"""Bounded process control. An old PID file never grants permission to signal a PID."""

import asyncio
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import httpx

SERVICES = {
    "napcat": (
        "NapCat",
        "https://napneko.github.io/guide/boot/Shell",
        "/",
        "WebUI 与反向 OneBot WS 分开配置；扫码在 NapCat 原生 WebUI 完成。",
    ),
    "comfyui": (
        "ComfyUI",
        "https://docs.comfy.org/installation/manual_install",
        "/system_stats",
        "使用独立虚拟环境；模型与工作流在 ComfyUI 安装目录管理。SoulBot 生图地址使用本机端口。",
    ),
    "meme": (
        "meme-generator",
        "https://github.com/MemeCrafters/meme-generator",
        "/meme/version",
        "建议优先使用 SoulBot 内置 meme 托管器。二者不能同时托管同一端口；已有进程只读。",
    ),
    "tts": (
        "本地 TTS",
        "https://github.com/remsky/Kokoro-FastAPI",
        "/v1/models",
        "支持管理员已安装的前台 TTS 服务。OpenAI 兼容服务在 SoulBot TTS 页单独配置；不预设所有 TTS 共用 API。",
    ),
    "llm": (
        "本地 LLM",
        "https://docs.ollama.com/cli",
        "/api/version",
        "Ollama 使用 serve 前台启动；OpenAI 兼容地址为 http://127.0.0.1:端口/v1。其他推理服务按其官方参数配置。",
    ),
    "custom": (
        "自定义服务",
        "",
        "/",
        "只启动管理员明确配置的本机前台可执行文件，不自动安装系统服务。",
    ),
}
SAFE_ENV = {
    "PATH",
    "HOME",
    "USER",
    "USERNAME",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "USERPROFILE",
    "LOCALAPPDATA",
}
LOG_LIMIT = 1024 * 1024


class ServiceController:
    def __init__(self, api):
        self.api, self.root = api, api.data_directory
        self.children, self.readers, self.reapers = {}, {}, {}
        self.environments, self.started = {}, {}
        self.locks = {key: asyncio.Lock() for key in SERVICES}
        self.closed = False
        self.launch_lock = asyncio.Lock()
        self.grace = {}

    def path(self, service, suffix):
        if service not in SERVICES:
            raise ValueError("未知服务")
        path = self.root / (service + suffix)
        if path.is_symlink():
            raise ValueError("数据文件不能是符号链接")
        return path

    def state(self, service):
        path = self.path(service, ".json")
        if not path.exists():
            return {}
        if path.stat().st_size > 8192:
            raise ValueError("进程记录过大")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("进程记录无效")
        return data

    def save(self, service, data):
        path = self.path(service, ".json")
        temp = self.path(service, ".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temp, path)
        path.chmod(0o600)

    @staticmethod
    def pid_alive(pid):
        if type(pid) is not int or pid <= 0:
            return False
        if os.name == "nt":
            # os.kill(pid, 0) is not a safe liveness probe on Windows.
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87
            try:
                code = wintypes.DWORD()
                return (
                    not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
                )
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    async def reachable(self, port):
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.4)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, TimeoutError):
            return False
        finally:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError

    def configuration(self, service):
        cfg = self.api.params
        executable = Path(cfg[service + "_executable"])
        directory = Path(cfg[service + "_directory"])
        if not cfg[service + "_enabled"]:
            raise ValueError("此服务尚未允许托管，请先配置并保存")
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("可执行文件必须是已安装的绝对路径")
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("工作目录必须是已存在的绝对路径")
        args = json.loads(cfg[service + "_arguments"])
        if (
            not isinstance(args, list)
            or len(args) > 60
            or any(not isinstance(v, str) or len(v) > 4096 or "\0" in v for v in args)
        ):
            raise ValueError("启动参数须为有界 JSON 字符串数组")
        extra = json.loads(cfg[service + "_environment"] or "{}")
        if (
            not isinstance(extra, dict)
            or len(extra) > 40
            or any(
                not isinstance(k, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", k)
                or not isinstance(v, str)
                or len(v) > 4096
                or "\0" in v
                for k, v in extra.items()
            )
        ):
            raise ValueError("环境变量须为 JSON 字符串对象")
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV}
        env.update(extra)
        env["PYTHONUNBUFFERED"] = "1"
        return [str(executable), *args], str(directory), env, extra

    async def rows(self):
        result = []
        cfg = self.api.params
        for service, (name, _, _, _) in SERVICES.items():
            process = self.children.get(service)
            old = self.state(service)
            owned = process is not None and process.returncode is None
            port = old.get("port", cfg[service + "_port"]) if owned else cfg[service + "_port"]
            reachable = await self.reachable(port)
            uncertain = (
                not owned and old.get("status") == "running" and self.pid_alive(old.get("pid"))
            )
            state = (
                "运行中"
                if owned and reachable
                else "启动中"
                if owned
                else "归属待核对"
                if uncertain
                else "外部服务在线"
                if reachable
                else "已退出"
                if old.get("exit_code") is not None
                else "未启动"
            )
            result.append(
                {
                    "service": service,
                    "name": name,
                    "state": state,
                    "reachable": reachable,
                    "owned": owned,
                    "pid": process.pid if owned else None,
                    "port": port,
                    "allowed": cfg[service + "_enabled"],
                    "started_at": self.started.get(service),
                    "exit_code": old.get("exit_code"),
                }
            )
        return result

    def cards(self, rows):
        return [
            {
                "title": row["name"],
                "status": row["state"],
                "subtitle": "本机 127.0.0.1:" + str(row["port"]),
                "fields": [
                    {"label": "托管许可", "value": "已开启" if row["allowed"] else "未开启"},
                    {"label": "进程归属", "value": "本插件" if row["owned"] else "外部或未启动"},
                    {"label": "PID", "value": str(row["pid"] or "—")},
                ],
                "actions": [
                    {"name": name, "values": {"service": row["service"]}}
                    for name in (
                        ["details", "logs", "stop", "restart"]
                        if row["owned"]
                        else ["details", "logs", "start"]
                    )
                ],
            }
            for row in rows
        ]

    async def overview(self):
        rows = await self.rows()
        return {
            "message": "只管理由本插件启动的前台进程；不接管外部服务。",
            "cards": self.cards(rows),
        }

    async def details(self, service):
        cfg = self.api.params
        row = next(row for row in await self.rows() if row["service"] == service)
        name, docs, endpoint, note = SERVICES[service]
        # HTTP discovery is bounded and never follows redirects or uploads credentials.
        health = {"tcp": row["reachable"], "http_status": None, "version": None}
        if row["reachable"]:
            try:
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=2
                ) as client:
                    async with client.stream(
                        "GET", f"http://127.0.0.1:{row['port']}{endpoint}"
                    ) as response:
                        health["http_status"] = response.status_code
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 262144:
                                raise ValueError("探测响应过大")
                        if response.status_code == 200 and service in {"meme", "comfyui", "llm"}:
                            value = json.loads(data)
                            version = (
                                value
                                if service == "meme" and isinstance(value, str)
                                else value.get("version")
                                if service == "llm" and isinstance(value, dict)
                                else value.get("system", {}).get("comfyui_version")
                                if service == "comfyui" and isinstance(value, dict)
                                else None
                            )
                            if isinstance(version, str):
                                health["version"] = version[:80]
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                health["note"] = "端口可达，但没有取得已识别的版本；不代表业务接口可用。"
        fields = [
            {"label": "说明", "value": note},
            {"label": "可执行文件", "value": cfg[service + "_executable"] or "未配置"},
            {"label": "工作目录", "value": cfg[service + "_directory"] or "未配置"},
            {"label": "健康检查", "value": json.dumps(health, ensure_ascii=False)},
            {
                "label": "启动时间",
                "value": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["started_at"]))
                if row["started_at"]
                else "—",
            },
        ]
        card = self.cards([row])[0]
        card["fields"] += fields
        card["links"] = ([{"label": "官方安装与配置说明", "url": docs}] if docs else []) + [
            {"label": "打开本地服务", "url": f"http://127.0.0.1:{row['port']}"}
        ]
        return {"message": name + " · TCP 可达与实际服务能力分别显示。", "cards": [card]}

    def redact(self, text, service):
        for value in self.environments.get(service, {}).values():
            if value:
                text = text.replace(value, "[已隐藏]")
        try:
            extra = json.loads(self.api.params[service + "_environment"] or "{}")
            for value in extra.values():
                if value:
                    text = text.replace(str(value), "[已隐藏]")
        except Exception:
            pass
        text = re.sub(
            r"(?i)(token|api[_-]?key|password|secret|authorization)([\s\"':=]+)[^\s,;\"<>]+",
            r"\1\2[已隐藏]",
            text,
        )
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        return text

    async def capture(self, service, process):
        path = self.path(service, ".log")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.chmod(path, 0o600)
        size = 0
        pending = b""
        dropping = False
        try:
            with os.fdopen(fd, "wb") as stream:
                while True:
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        break
                    pending += chunk
                    if dropping:
                        if b"\n" not in pending:
                            pending = b""
                            continue
                        pending = pending.split(b"\n", 1)[1]
                        dropping = False
                    while b"\n" in pending or len(pending) > 16384:
                        if b"\n" in pending and pending.index(b"\n") <= 16384:
                            line, pending = pending.split(b"\n", 1)
                            safe = (
                                self.redact(line.decode("utf-8", "replace"), service).encode()
                                + b"\n"
                            )
                        else:
                            # Drop the whole overlong line, including its eventual suffix.
                            if b"\n" in pending:
                                pending = pending.split(b"\n", 1)[1]
                            else:
                                pending = b""
                                dropping = True
                            safe = b"[overlong output omitted]\n"
                        if size + len(safe) > LOG_LIMIT:
                            stream.seek(0)
                            stream.truncate()
                            size = 0
                        stream.write(safe)
                        stream.flush()
                        size += len(safe)
                if pending and not dropping:
                    safe = self.redact(pending.decode("utf-8", "replace"), service).encode()
                    if size + len(safe) <= LOG_LIMIT:
                        stream.write(safe)
        except OSError:
            # Continue draining output even when logging fails, to avoid blocking the service.
            while await process.stdout.read(4096):
                pass

    async def reap(self, service, process):
        code = await process.wait()
        old = self.state(service)
        if old.get("pid") == process.pid:
            old.update(status="exited", exit_code=code)
            self.save(service, old)

    async def start(self, service):
        async with self.locks[service]:
            return await self._start(service)

    async def _start(self, service):
        if self.closed:
            raise ValueError("插件正在关闭")
        existing = self.children.get(service)
        if existing and existing.returncode is None:
            return {
                "message": "服务已经由本插件启动，没有重复创建进程。",
                "cards": self.cards(
                    [row for row in await self.rows() if row["service"] == service]
                ),
            }
        argv, directory, env, extra = self.configuration(service)
        old = self.state(service)
        if old.get("status") == "running" and self.pid_alive(old.get("pid")):
            raise ValueError("旧进程归属不确定，请人工核对；不会凭 PID 文件停止进程")
        async with self.launch_lock:
            port = self.api.params[service + "_port"]
            if await self.reachable(port):
                raise ValueError("端口已有外部服务，不启动重复实例")
            for key, proc in self.children.items():
                if proc.returncode is None and self.state(key).get("port") == port:
                    raise ValueError("另一个受管进程正在使用此端口")
            # Clean old completed reader/reaper before overwriting state.
            await self.finish_tasks(service)
            self.environments[service] = extra
            child = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=directory,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **(
                        {"start_new_session": True}
                        if os.name != "nt"
                        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    ),
                )
            )
            try:
                process = await asyncio.shield(child)
            except asyncio.CancelledError:
                process = await child
                self.children[service] = process
                self.readers[service] = asyncio.create_task(self.capture(service, process))
                await self.terminate(service, process)
                await self.finish_tasks(service)
                raise
            self.children[service] = process
        self.grace[service] = self.api.params["stop_seconds"]
        self.started[service] = time.time()
        try:
            self.save(
                service,
                {
                    "status": "running",
                    "pid": process.pid,
                    "port": port,
                    "started_at": self.started[service],
                },
            )
            self.readers[service] = asyncio.create_task(self.capture(service, process))
            self.reapers[service] = asyncio.create_task(self.reap(service, process))
            deadline = time.monotonic() + self.api.params["startup_seconds"]
            while process.returncode is None and time.monotonic() < deadline:
                if await self.reachable(port):
                    break
                await asyncio.sleep(0.2)
            message = (
                "进程已退出，请查看脱敏日志。"
                if process.returncode is not None
                else "服务端口已就绪；业务能力请查看详细信息。"
                if await self.reachable(port)
                else "进程仍在启动，等待超时；已保留进程，请稍后刷新，不会重复启动。"
            )
            return {
                "message": message,
                "cards": self.cards(
                    [row for row in await self.rows() if row["service"] == service]
                ),
            }
        except BaseException:
            await self.terminate(service, process)
            raise

    async def terminate(self, service, process):
        if process.returncode is None:
            try:
                if os.name != "nt" and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.grace.get(service, 5))
            except TimeoutError:
                try:
                    if os.name != "nt" and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        self.save(
            service, {**self.state(service), "status": "exited", "exit_code": process.returncode}
        )

    async def finish_tasks(self, service):
        tasks = [task for task in (self.readers.get(service), self.reapers.get(service)) if task]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
            except TimeoutError:
                pass
        self.readers.pop(service, None)
        self.reapers.pop(service, None)

    async def stop(self, service):
        async with self.locks[service]:
            return await self._stop(service)

    async def _stop(self, service):
        process = self.children.get(service)
        if process is None:
            return {"message": "没有本插件持有的进程；没有停止外部服务。"}
        await self.terminate(service, process)
        await self.finish_tasks(service)
        self.children.pop(service, None)
        return {
            "message": "本插件持有的进程已停止；外部服务未被接管。",
            "cards": self.cards([row for row in await self.rows() if row["service"] == service]),
        }

    async def restart(self, service):
        async with self.locks[service]:
            if service not in self.children:
                raise ValueError("只能重启当前受管进程；请先配置并明确启动")
            self.configuration(service)
            await self._stop(service)
            return await self._start(service)

    def logs(self, service):
        path = self.path(service, ".log")
        if not path.exists():
            return {"message": "没有本插件采集的日志。", "text": ""}
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 16384))
            text = stream.read(16384).decode("utf-8", "replace")
        return {
            "message": SERVICES[service][0] + " · 最近脱敏日志",
            "text": self.redact(text, service),
        }

    async def close(self):
        self.closed = True
        await asyncio.gather(
            *(self.stop(key) for key in list(self.children)), return_exceptions=True
        )
