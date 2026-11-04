"""Official, pinned downloads and bounded source ZIP extraction; never executes installers."""

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import time
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

REPOSITORIES = {
    "napcat": "NapNeko/NapCatQQ",
    "comfyui": "Comfy-Org/ComfyUI",
    "meme": "MemeCrafters/meme-generator",
    "tts": "remsky/Kokoro-FastAPI",
    "llm": "ollama/ollama",
}
HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
DOCS = {
    "napcat": "https://napneko.github.io/guide/boot/Shell",
    "comfyui": "https://docs.comfy.org/installation/manual_install",
    "meme": "https://github.com/MemeCrafters/meme-generator/tree/v0.1.14",
    "tts": "https://github.com/remsky/Kokoro-FastAPI",
    "llm": "https://docs.ollama.com/cli",
}


class Packages:
    def __init__(self, api):
        self.api = api
        self.root = api.data_directory / "packages"
        if self.root.is_symlink():
            raise ValueError("下载目录不能是符号链接")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.task = None
        self.active = None
        self.extracting = None
        self.extraction_task = None
        self.previous = {}
        self.transport = None
        for service in REPOSITORIES:
            state = self.state(service)
            if state.get("status") in {
                "queued",
                "resolving",
                "downloading",
                "unpacking",
            }:
                state.update(status="interrupted", message="上次任务中断，请检查后重新下载。")
                self.save(service, state)
            for path in self.root.glob(service + "-*.part"):
                if path.is_symlink():
                    raise ValueError("暂存文件路径无效")
                path.unlink()

    def state(self, service):
        if service not in REPOSITORIES:
            raise ValueError("自定义服务请使用其官方安装说明")
        path = self.root / (service + ".json")
        if path.is_symlink():
            raise ValueError("状态文件不能是符号链接")
        return (
            json.loads(path.read_text())
            if path.exists()
            else {"service": service, "status": "not_downloaded"}
        )

    def save(self, service, value):
        path = self.root / (service + ".json")
        temp = self.root / (service + ".tmp")
        if path.is_symlink() or temp.is_symlink():
            raise ValueError("状态文件路径无效")
        with os.fdopen(os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp, path)

    @staticmethod
    def valid_url(url):
        value = urlsplit(url)
        if (
            value.scheme != "https"
            or value.hostname not in HOSTS
            or value.username
            or value.password
            or value.port not in {None, 443}
        ):
            raise ValueError("下载地址不是允许的官方来源")
        return url

    async def metadata(self, path):
        async with (
            httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=30,
            ) as c,
            c.stream(
                "GET",
                "https://api.github.com/" + path,
                headers={"Accept": "application/vnd.github+json"},
            ) as r,
        ):
            r.raise_for_status()
            body = bytearray()
            async for chunk in r.aiter_bytes(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("上游元数据过大")
        return json.loads(body)

    async def resolve(self, service, variant):
        repo = REPOSITORIES[service]
        if service in {"napcat", "llm"} and variant != "source":
            release = await self.metadata("repos/" + repo + "/releases/latest")
            system = platform.system().lower()
            machine = platform.machine().lower()
            if service == "napcat":
                names = (
                    ["NapCat.Shell.Windows.Node.zip"]
                    if variant == "windows" or variant == "auto" and system == "windows"
                    else ["NapCat.Shell.zip"]
                )
            elif variant == "windows" or variant == "auto" and system == "windows":
                names = ["OllamaSetup.exe", "ollama-windows-amd64.zip"]
            elif variant == "macos" or variant == "auto" and system == "darwin":
                names = ["ollama-darwin.tgz", "ollama-darwin.zip", "Ollama-darwin.zip"]
            else:
                arch = (
                    "arm64"
                    if variant == "linux_arm64"
                    or variant == "auto"
                    and machine in {"arm64", "aarch64"}
                    else "amd64"
                )
                names = [
                    "ollama-linux-" + arch + ".tar.zst",
                    "ollama-linux-" + arch + ".tgz",
                ]
            assets = {a["name"]: a for a in release.get("assets", [])}
            asset = next((assets[name] for name in names if name in assets), None)
            if not asset:
                raise ValueError("官方发行版没有匹配的平台文件，请查看官方下载页")
            digest = asset.get("digest") or ""
            return {
                "url": self.valid_url(asset["browser_download_url"]),
                "version": release["tag_name"],
                "filename": asset["name"],
                "kind": "release",
                "expected_bytes": asset["size"],
                "expected_sha256": digest[7:]
                if re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                else None,
            }
        ref = "v0.1.14" if service == "meme" else "HEAD"
        value = await self.metadata("repos/" + repo + "/commits/" + quote(ref, safe=""))
        sha = value["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("上游提交标识无效")
        return {
            "url": f"https://codeload.github.com/{repo}/zip/{sha}",
            "version": ref if ref != "HEAD" else sha[:12],
            "commit": sha,
            "filename": service + "-" + sha + ".zip",
            "kind": "source",
            "expected_bytes": None,
            "expected_sha256": None,
        }

    def begin(self, service, variant):
        self.previous[service] = self.state(service)
        if self.extracting:
            raise ValueError("请先完成解压")
        if self.task and not self.task.done():
            if self.active == service:
                return self.status(service)
            raise ValueError("已有下载或解压任务，请完成或取消后再操作")
        self.active = service
        self.save(
            service,
            {"service": service, "status": "queued", "bytes": 0, "variant": variant},
        )
        self.task = asyncio.create_task(self.run_download(service, variant))
        return self.status(service)

    async def run_download(self, service, variant):
        try:
            await asyncio.wait_for(self.download(service, variant), 3600)
        except TimeoutError:
            state = self.state(service)
            state.update(status="failed", message="下载超过一小时，请核对网络后重试。")
            self.save(service, state)

    async def download(self, service, variant):
        temporary = None
        state = {
            "service": service,
            "status": "resolving",
            "bytes": 0,
            "variant": variant,
        }
        self.save(service, state)
        try:
            info = await self.resolve(service, variant)
            maximum = self.api.params["download_max_mb"] * 1024 * 1024
            if info["expected_bytes"] and info["expected_bytes"] > maximum:
                raise ValueError("发行包超过配置下载上限")
            if shutil.disk_usage(self.root).free < maximum + 64 * 1024 * 1024:
                raise ValueError("磁盘可用空间不足")
            filename = info["filename"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", filename):
                raise ValueError("下载文件名无效")
            # Versions with the same upstream filename are still isolated by a URL hash.
            target = self.root / (
                service
                + "-"
                + hashlib.sha256(info["url"].encode()).hexdigest()[:16]
                + "-"
                + filename
            )
            temporary = target.with_suffix(target.suffix + ".part")
            if target.is_symlink() or temporary.is_symlink():
                raise ValueError("下载文件路径无效")
            state.update(info, status="downloading", path=str(target))
            self.save(service, state)
            if target.exists():
                digest = await asyncio.to_thread(self.digest, target)
                expected = info["expected_sha256"] or self.previous.get(service, {}).get("sha256")
                if not expected or digest != expected:
                    raise ValueError(
                        "已下载文件缺少可核对摘要或内容已改变，请先人工核对并移走该文件"
                    )
                state.update(
                    status="downloaded",
                    bytes=target.stat().st_size,
                    sha256=digest,
                    message="文件已存在，未重复下载。",
                )
                return
            hasher = hashlib.sha256()
            url = info["url"]
            last = 0
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=60,
            ) as client:
                for redirect in range(6):
                    self.valid_url(url)
                    async with client.stream("GET", url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = str(response.url.join(response.headers["location"]))
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length and int(length) > maximum:
                            raise ValueError("下载超过配置上限")
                        with os.fdopen(
                            os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                            "wb",
                        ) as stream:
                            async for chunk in response.aiter_bytes(65536):
                                state["bytes"] += len(chunk)
                                if state["bytes"] > maximum:
                                    raise ValueError("下载超过配置上限")
                                stream.write(chunk)
                                hasher.update(chunk)
                                if time.monotonic() - last > 1:
                                    self.save(service, state)
                                    last = time.monotonic()
                        break
                else:
                    raise ValueError("下载重定向过多")
            if info["expected_bytes"] is not None and state["bytes"] != info["expected_bytes"]:
                raise ValueError("下载长度不匹配")
            if not state["bytes"]:
                raise ValueError("下载内容为空")
            digest = hasher.hexdigest()
            if info["expected_sha256"] and digest != info["expected_sha256"]:
                raise ValueError("上游 SHA-256 校验失败")
            os.replace(temporary, target)
            state.update(
                status="downloaded",
                sha256=digest,
                message="下载完成；源码请解压，原生发行包请按配置引导安装。",
            )
        except asyncio.CancelledError:
            state.update(status="cancelled", message="下载已取消，未启动任何服务。")
            raise
        except Exception as error:
            state.update(
                status="failed",
                message="下载未完成："
                + (
                    "网络请求失败，请核对网络或 GitHub 限额。"
                    if isinstance(error, httpx.HTTPError)
                    else str(error)
                    if isinstance(error, ValueError)
                    else "请检查磁盘空间及目录权限。"
                ),
            )
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
            self.save(service, state)

    @staticmethod
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    async def unpack(self, service):
        if self.extracting:
            raise ValueError("已有解压任务")
        state = self.state(service)
        if state.get("status") != "downloaded" or state.get("kind") != "source":
            raise ValueError("只解压已经下载完成的源码 ZIP，原生发行包请按官方引导安装")
        from pathlib import Path

        archive = Path(state["path"])
        if (
            archive.parent != self.root
            or archive.is_symlink()
            or await asyncio.to_thread(self.digest, archive) != state["sha256"]
        ):
            raise ValueError("源码包已改变，请重新下载")
        dest = self.root / (service + "-" + state["commit"][:16] + "-source")
        if dest.exists():
            raise ValueError("解压目录已存在，不覆盖；请在引导中查看路径")
        stage = self.root / (service + "-extracting")
        if stage.exists():
            raise ValueError("存在未清理的解压暂存目录，请核对后处理")
        stage.mkdir(mode=0o700)
        self.extracting = service
        self.extraction_task = asyncio.current_task()
        try:
            with zipfile.ZipFile(archive) as z:
                items = z.infolist()
                if len(items) > 30000 or sum(i.file_size for i in items) > 2 * 1024**3:
                    raise ValueError("解压文件数量或体积超过上限")
                seen = set()
                for item in items:
                    path = PurePosixPath(item.filename)
                    if (
                        path.is_absolute()
                        or "\\" in item.filename
                        or ":" in item.filename
                        or any(p in {".", ".."} for p in path.parts)
                        or path.as_posix().rstrip("/") != item.filename.rstrip("/")
                        or item.filename in seen
                        or item.flag_bits & 1
                        or (item.external_attr >> 16) & 0o170000 == 0o120000
                    ):
                        raise ValueError("源码包包含无效路径或符号链接")
                    seen.add(item.filename)
                    out = stage.joinpath(*path.parts)
                    if item.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        z.open(item) as source,
                        os.fdopen(
                            os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                            "wb",
                        ) as target,
                    ):
                        while chunk := source.read(65536):
                            target.write(chunk)
                            await asyncio.sleep(0)
            os.replace(stage, dest)
            children = list(dest.iterdir())
            directory = children[0] if len(children) == 1 and children[0].is_dir() else dest
            state.update(directory=str(directory))
            self.save(service, state)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            raise ValueError("源码 ZIP 损坏、已加密或压缩格式不受支持") from error
        finally:
            self.extracting = None
            self.extraction_task = None
            if stage.exists():
                shutil.rmtree(stage)
        return self.guide(service)

    async def cancel(self, service):
        if self.extracting == service and self.extraction_task:
            task = self.extraction_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.active == service and self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return self.status(service)

    def status(self, service):
        state = self.state(service)
        labels = {
            "version": "上游版本/提交",
            "bytes": "已下载字节",
            "expected_bytes": "上游文件字节数",
            "sha256": "SHA-256",
            "path": "下载文件位置",
            "directory": "源码目录",
            "message": "处理结果",
        }
        fields = [
            {"label": labels[k], "value": str(state.get(k, "—"))}
            for k in (
                "version",
                "bytes",
                "expected_bytes",
                "sha256",
                "path",
                "directory",
                "message",
            )
        ]
        return {
            "message": "下载只准备文件；依赖安装、登录和模型准备请按引导继续。",
            "cards": [
                {
                    "title": {
                        "napcat": "NapCat",
                        "comfyui": "ComfyUI",
                        "meme": "meme-generator",
                        "tts": "本地 TTS",
                        "llm": "本地 LLM",
                    }[service],
                    "status": {
                        "queued": "等待下载",
                        "resolving": "读取官方版本",
                        "downloading": "下载中",
                        "downloaded": "下载完成",
                        "failed": "未完成",
                        "cancelled": "已取消",
                        "interrupted": "上次任务中断",
                        "not_downloaded": "尚未下载",
                    }.get(state["status"], state["status"]),
                    "fields": fields,
                    "actions": [
                        {"name": name, "values": {"service": service}}
                        for name in (
                            "download_status",
                            "guide",
                            "cancel_download",
                            "unpack",
                        )
                    ],
                }
            ],
        }

    def guide(self, service):
        state = self.state(service)
        directory = state.get("directory", "<解压后的服务目录>")
        win = platform.system() == "Windows"
        python = directory + ("/.venv/Scripts/python.exe" if win else "/.venv/bin/python")
        port = self.api.params[service + "_port"]
        url = f"http://127.0.0.1:{port}"
        shared = [
            "1. 点击“下载服务”，选择 auto 或 source；等待下载状态为 downloaded。",
            "2. 源码包点击“解压源码”。原生安装包按官方说明安装；下载不会自动执行安装程序。",
            "3. 在服务目录创建独立虚拟环境并按官方说明安装依赖；模型文件按许可证和硬件另行准备。",
            "4. 填写下方启动配置示例，保存插件参数，再手动启动；服务就绪后配置 SoulBot 对应 Provider。",
        ]
        args = []
        executable = python
        extra = {}
        if service == "comfyui":
            args = ["main.py", "--listen", "127.0.0.1", "--port", str(port)]
            shared += [
                "ComfyUI：先按官方硬件说明安装 PyTorch，再安装 requirements.txt；把 checkpoint 放到 models/checkpoints。在 SoulBot 图片生成页填 "
                + url
                + "，读取实际模型并选择 checkpoint。"
            ]
        elif service == "meme":
            executable = directory + ("/.venv/Scripts/meme.exe" if win else "/.venv/bin/meme")
            args = ["run"]
            shared += [
                "meme-generator：使用 Python 3.11 独立环境安装 meme-generator==0.1.14，按上游说明准备资源和 config.toml。server.host 设为 127.0.0.1，端口与检测端口一致。已启用 SoulBot 内置托管时请继续用内置托管，避免重复启动。"
            ]
        elif service == "napcat":
            executable = "<NapCat 已安装的前台入口>"
            shared += [
                "NapCat：按当前 Windows/macOS/Linux 的官方安装步骤准备匹配 QQ；Shell ZIP 不等于完整 QQ 安装包。扫码登录后，在 NapCat WebUI 添加反向 WS 客户端，填写 SoulBot“QQ 连接”显示的接收地址，并核对双方 Token。"
            ]
        elif service == "tts":
            executable = "<TTS 官方文档指定的前台入口>"
            shared += [
                "本地 TTS 默认下载 Kokoro-FastAPI 源码；按官方 Native/UV 或容器安装说明准备推理依赖和语音模型。不要使用会立即返回的后台启动器。兼容 OpenAI 语音接口时，在 SoulBot TTS 页填写 "
                + url
                + "/v1，选择真实语音并试合成。"
            ]
        elif service == "llm":
            executable = "<安装后的 ollama 可执行文件绝对路径>"
            args = ["serve"]
            extra = {"OLLAMA_HOST": f"127.0.0.1:{port}"}
            shared += [
                "Ollama：原生发行包按官方说明安装。使用 ollama pull 模型ID 下载你选择的模型，先核对模型实际内存/磁盘要求；源码包需要官方 Go/C++ 构建工具链。SoulBot 模型页填写 "
                + url
                + "/v1，读取模型列表并测试工具调用能力。"
            ]
        fields = [
            {
                "label": "当前平台",
                "value": platform.system() + " / " + platform.machine(),
            },
            {"label": "已下载路径", "value": state.get("path", "尚未下载")},
            {"label": "已解压目录", "value": state.get("directory", "尚未解压")},
        ]
        return {
            "message": "按顺序下载、安装依赖、配置、启动并验证；存在的服务不会自动被接管。",
            "cards": [
                {
                    "title": {
                        "napcat": "NapCat",
                        "comfyui": "ComfyUI",
                        "meme": "meme-generator",
                        "tts": "本地 TTS",
                        "llm": "本地 LLM",
                    }[service],
                    "subtitle": "下载与配置引导",
                    "fields": fields,
                    "links": [{"label": "官方完整安装说明", "url": DOCS[service]}],
                    "actions": [
                        {"name": "download", "values": {"service": service, "variant": "auto"}},
                        *[
                            {"name": name, "values": {"service": service}}
                            for name in ("download_status", "unpack")
                        ],
                    ],
                }
            ],
            "text": "\n\n".join(shared)
            + "\n\n创建虚拟环境示例（在服务目录执行）：\npython -m venv .venv\n\n填写到插件配置中的建议值（请先完成安装）：\n"
            + json.dumps(
                {
                    service + "_enabled": True,
                    service + "_directory": directory,
                    service + "_executable": executable,
                    service + "_arguments": json.dumps(args, ensure_ascii=False),
                    service + "_port": port,
                    service + "_environment": json.dumps(extra),
                },
                ensure_ascii=False,
                indent=2,
            ),
        }

    async def close(self):
        if self.extraction_task:
            self.extraction_task.cancel()
            await asyncio.gather(self.extraction_task, return_exceptions=True)
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
