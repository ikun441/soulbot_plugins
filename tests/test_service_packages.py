import asyncio
import hashlib
import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest
import yaml
from test_service_host import PACKAGE, module


@pytest.fixture
def packages(tmp_path):
    manifest = yaml.safe_load((PACKAGE / "config.yaml").read_text())
    params = {k: v["default"] for k, v in manifest["parameters"].items()}
    from importlib import import_module

    cls = import_module(module.__package__ + ".packages").Packages
    return cls(SimpleNamespace(params=params, data_directory=tmp_path))


def archive(entries=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name, body in (
            entries
            or {
                "example/README.md": "source only",
                "example/main.py": 'raise RuntimeError("must never execute")',
            }
        ).items():
            z.writestr(name, body)
    return output.getvalue()


def wire(packages, body):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(200, content=body)

    packages.transport = httpx.MockTransport(respond)
    return calls


def test_pinned_source_download_unpacks_and_guides_without_executing(packages):
    body = archive()
    calls = wire(packages, body)

    async def run():
        packages.begin("comfyui", "auto")
        await packages.task
        state = packages.state("comfyui")
        assert (
            state["status"] == "downloaded" and state["sha256"] == hashlib.sha256(body).hexdigest()
        )
        assert any("/" + "a" * 40 in url for url in calls)
        result = await packages.unpack("comfyui")
        assert "models/checkpoints" in result["text"] and "8188" in result["text"]
        assert packages.state("comfyui")["directory"].endswith("/example")
        packages.begin("comfyui", "auto")
        await packages.task
        assert packages.state("comfyui")["status"] == "downloaded"

    asyncio.run(run())


def test_archive_tampering_and_traversal_rejected(packages, tmp_path):
    wire(packages, archive({"../escaped.txt": "bad"}))

    async def run():
        packages.begin("meme", "source")
        await packages.task
        with pytest.raises(ValueError, match="无效路径"):
            await packages.unpack("meme")
        assert not (tmp_path / "escaped.txt").exists()
        path = packages.root / packages.state("meme")["path"].split("/")[-1]
        path.write_bytes(b"changed")
        packages.begin("meme", "source")
        await packages.task
        assert packages.state("meme")["status"] == "failed"

    asyncio.run(run())


def test_nonofficial_redirect_is_never_requested(packages):
    requests = []

    def respond(request):
        requests.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(302, headers={"location": "http://127.0.0.1:6099/private"})

    packages.transport = httpx.MockTransport(respond)

    async def run():
        packages.begin("tts", "auto")
        await packages.task
        assert packages.state("tts")["status"] == "failed"
        assert "127.0.0.1" not in requests
        assert not list(packages.root.glob("*.part"))

    asyncio.run(run())


def test_release_platform_selection_and_upstream_digest(packages):
    body = b"not an executable fixture"
    digest = hashlib.sha256(body).hexdigest()

    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.2.3",
                    "assets": [
                        {
                            "name": "OllamaSetup.exe",
                            "size": len(body),
                            "digest": "sha256:" + digest,
                            "browser_download_url": "https://github.com/ollama/ollama/releases/download/v1.2.3/OllamaSetup.exe",
                        }
                    ],
                },
            )
        return httpx.Response(200, content=body)

    packages.transport = httpx.MockTransport(respond)

    async def run():
        packages.begin("llm", "windows")
        await packages.task
        state = packages.state("llm")
        assert (
            state["status"] == "downloaded"
            and state["expected_sha256"] == state["sha256"] == digest
        )
        with pytest.raises(ValueError, match="原生发行包"):
            await packages.unpack("llm")

    asyncio.run(run())


def test_cancel_preserves_state_and_cleans_partial_download(packages):
    started = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 65536
            started.set()
            await asyncio.Event().wait()

    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(200, stream=Stream())

    packages.transport = httpx.MockTransport(respond)

    async def run():
        packages.begin("comfyui", "source")
        await started.wait()
        with pytest.raises(ValueError):
            packages.begin("meme", "auto")
        await packages.cancel("comfyui")
        assert packages.state("comfyui")["status"] == "cancelled"
        assert not list(packages.root.glob("*.part"))

    asyncio.run(run())
    packages.save("meme", {"service": "meme", "status": "downloading"})
    recovered = type(packages)(packages.api)
    assert recovered.state("meme")["status"] == "interrupted"


def test_all_guides_are_real_profiles_with_bounded_schema(packages):
    for name in ("napcat", "comfyui", "meme", "tts", "llm"):
        guide = packages.guide(name)
        assert guide["cards"][0]["links"][0]["url"].startswith("https://")
        assert name + "_executable" in guide["text"]
    with pytest.raises(ValueError):
        packages.guide("custom")
