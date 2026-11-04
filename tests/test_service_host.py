"""Real disposable foreground processes; never start/stop the user's third-party services."""

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "plugins/service_host"
spec = importlib.util.spec_from_file_location(
    "service_host_fixture", PACKAGE / "plugin.py", submodule_search_locations=[str(PACKAGE)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Controller = module.ServiceController


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@pytest.fixture
def setup(tmp_path):
    manifest = yaml.safe_load((PACKAGE / "config.yaml").read_text())
    params = {key: field["default"] for key, field in manifest["parameters"].items()}
    params.update(startup_seconds=1, stop_seconds=1)
    # Each profile uses a unique, unprivileged port to avoid touching installed services.
    for service in ("napcat", "comfyui", "meme", "tts", "llm", "custom"):
        params[service + "_port"] = free_port()
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    server = tmp_path / "server.py"
    server.write_text("""import asyncio, os, sys
async def main():
    print("private="+os.environ.get("SERVICE_TEST_SECRET", "none"),flush=True)
    print("host_key_present="+str("SOULBOT_LLM_API_KEY" in os.environ),flush=True)
    server=await asyncio.start_server(lambda r,w:w.close(), "127.0.0.1", int(sys.argv[1]))
    async with server: await server.serve_forever()
asyncio.run(main())
""")
    params.update(
        custom_enabled=True,
        custom_executable=sys.executable,
        custom_directory=str(tmp_path),
        custom_arguments=json.dumps([str(server), str(params["custom_port"])]),
        custom_environment=json.dumps({"SERVICE_TEST_SECRET": "private-fixture-value"}),
    )
    api = SimpleNamespace(params=params, data_directory=data)
    return Controller(api), params, server


def test_real_process_idempotency_restart_cleanup_and_private_logs(setup, monkeypatch):
    controller, cfg, _ = setup
    monkeypatch.setenv("SOULBOT_LLM_API_KEY", "must-not-reach-child")

    async def scenario():
        try:
            await asyncio.gather(controller.start("custom"), controller.start("custom"))
            first = controller.children["custom"]
            assert first.returncode is None
            assert await controller.reachable(cfg["custom_port"])
            text = controller.logs("custom")["text"]
            assert "private-fixture-value" not in text
            assert "[已隐藏]" in text and "host_key_present=False" in text
            await controller.restart("custom")
            assert first.returncode is not None
            assert controller.children["custom"].pid != first.pid
            if os.name != "nt":
                assert controller.path("custom", ".json").stat().st_mode & 0o777 == 0o600
                assert controller.path("custom", ".log").stat().st_mode & 0o777 == 0o600
        finally:
            child = controller.children.get("custom")
            await controller.close()
        assert child.returncode is not None
        assert not await controller.reachable(cfg["custom_port"])

    asyncio.run(scenario())


def test_external_service_and_stale_pid_never_signalled(setup):
    controller, cfg, _ = setup

    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", cfg["custom_port"])
        async with server:
            with pytest.raises(ValueError, match="外部服务"):
                await controller.start("custom")
            await controller.stop("custom")
            assert await controller.reachable(cfg["custom_port"])
        controller.save(
            "custom", {"status": "running", "pid": os.getpid(), "port": cfg["custom_port"]}
        )
        with pytest.raises(ValueError, match="归属不确定"):
            await controller.start("custom")
        await controller.stop("custom")
        assert controller.pid_alive(os.getpid())

    asyncio.run(scenario())


def test_crash_is_recorded_and_shell_text_is_not_interpreted(setup):
    controller, cfg, _ = setup
    marker = Path(cfg["custom_directory"]) / "injected"
    cfg["custom_arguments"] = json.dumps(["-c", "import sys;sys.exit(7)", ";touch " + str(marker)])

    async def scenario():
        try:
            await controller.start("custom")
            assert controller.children["custom"].returncode == 7
            await asyncio.sleep(0.05)
            assert controller.state("custom")["exit_code"] == 7
            assert not marker.exists()
        finally:
            await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("_attempt", range(10))
def test_cancelled_start_closes_owned_process(setup, _attempt):
    controller, cfg, _ = setup
    cfg["custom_arguments"] = json.dumps(["-c", "import time;time.sleep(30)"])
    cfg["startup_seconds"] = 20

    async def scenario():
        task = asyncio.create_task(controller.start("custom"))
        while "custom" not in controller.children:
            await asyncio.sleep(0.01)
        child = controller.children["custom"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await controller.close()
        assert child.returncode is not None

    asyncio.run(scenario())


def test_invalid_config_and_conflicting_profiles(setup):
    controller, cfg, server = setup
    cfg["custom_arguments"] = '"not-an-array"'
    with pytest.raises(ValueError):
        controller.configuration("custom")
    cfg["custom_arguments"] = json.dumps([str(server), str(cfg["custom_port"])])
    for field in ("enabled", "executable", "directory", "arguments", "environment", "port"):
        cfg["tts_" + field] = cfg["custom_" + field]

    async def scenario():
        try:
            results = await asyncio.gather(
                controller.start("custom"), controller.start("tts"), return_exceptions=True
            )
            assert sum(isinstance(r, ValueError) for r in results) == 1
            assert sum(p.returncode is None for p in controller.children.values()) == 1
        finally:
            await controller.close()

    asyncio.run(scenario())


def test_actual_host_plugin_sdk_and_stop_on_disable(tmp_path, setup):
    from soulbot.app import build_services

    controller, cfg, _ = setup
    services = build_services(tmp_path / "host")
    shutil.copytree(
        PACKAGE,
        services.plugins.root / "service_host",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    secrets = {k: v for k, v in cfg.items() if k.endswith("_environment")}
    values = {k: v for k, v in cfg.items() if k not in secrets}

    async def scenario():
        services.plugins.configure("service_host", 0, values, secrets, [], "approval")
        await services.plugins.enable("service_host", 1)
        item = services.plugins.listing()["items"][0]
        assert len(item["admin_actions"]) == 6 and item["enabled"]
        assert item["secrets"]["custom_environment"] and "private-fixture-value" not in json.dumps(
            item
        )
        assert len(item["tools"]) == 1 and item["tools"][0].endswith("__status")
        try:
            result = await services.plugins.admin_execute(
                "service_host", "start", 2, {"service": "custom"}, True
            )
            assert result["cards"][0]["status"] == "运行中"
        finally:
            await services.plugins.disable("service_host", 2)
        assert not await controller.reachable(cfg["custom_port"])

    asyncio.run(scenario())
