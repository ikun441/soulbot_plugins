"""Administrator-only foreground service hosting; model access is status-only."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .controller import ServiceController


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceArgs(Empty):
    service: Literal["napcat", "comfyui", "meme", "tts", "llm", "custom"]


def register(api):
    if not hasattr(api, "admin_action") or not hasattr(api, "data_directory"):
        raise ValueError("第三方服务托管需要 SoulBot v2.0.4 或更新版本")
    controller = ServiceController(api)
    api.on_close(controller.close)

    async def overview(args):
        return await controller.overview()

    async def details(args):
        return await controller.details(args.service)

    async def logs(args):
        return controller.logs(args.service)

    async def start(args):
        return await controller.start(args.service)

    async def stop(args):
        return await controller.stop(args.service)

    async def restart(args):
        return await controller.restart(args.service)

    for name, label, description, schema, handler, mutating in [
        (
            "overview",
            "刷新服务列表",
            "读取本地端口和本插件进程状态，不启动服务。",
            Empty,
            overview,
            False,
        ),
        (
            "details",
            "详细信息与健康检查",
            "查看服务配置摘要、官方指引及本机 API 能力，不修改服务。",
            ServiceArgs,
            details,
            False,
        ),
        (
            "logs",
            "脱敏日志",
            "查看本插件采集的最近日志；不读取外部服务日志文件。",
            ServiceArgs,
            logs,
            False,
        ),
        (
            "start",
            "启动服务",
            "使用管理员已保存的配置启动前台进程；拒绝重复启动和占用端口。",
            ServiceArgs,
            start,
            True,
        ),
        (
            "stop",
            "停止服务",
            "仅停止本插件当前持有的子进程，不终止外部服务。",
            ServiceArgs,
            stop,
            True,
        ),
        (
            "restart",
            "重启服务",
            "先停止当前受管进程，再按已保存配置启动；外部服务不能重启。",
            ServiceArgs,
            restart,
            True,
        ),
    ]:
        api.admin_action(
            name=name,
            label=label,
            description=description,
            schema=schema,
            handler=handler,
            mutating=mutating,
            timeout=70,
        )

    async def model_status(args, context):
        # Do not expose command lines, environment, paths or logs to the model.
        return {
            "services": [
                {
                    k: value
                    for k, value in row.items()
                    if k in {"service", "name", "state", "reachable"}
                }
                for row in await controller.rows()
            ]
        }

    api.tool(
        name="status",
        description="只读查询本机第三方服务状态；不能启动、停止或修改服务。",
        schema=Empty,
        handler=model_status,
        permission="approval",
        timeout=10,
        max_calls=2,
    )
