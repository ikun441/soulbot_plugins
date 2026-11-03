# SoulBot 插件仓库

用于 SoulBot 的单文件夹插件与市场索引。插件开发契约见 [PLUGIN_SPEC.md](PLUGIN_SPEC.md)。

## 已发布插件

| 插件 | 版本 | 宿主要求 | 功能 |
| --- | --- | --- | --- |
| [第三方服务托管](plugins/service_host/README.md) | v0.1.0 | SoulBot v2.0.4+ | NapCat、ComfyUI、meme-generator、本地 TTS/LLM 与自定义服务的配置、启动、停止、重启、状态、详情和脱敏日志 |

在 SoulBot“插件市场”刷新索引并安装，核对来源后启用。服务不会随安装自动启动，实际启动须管理员保存配置并确认。

每个插件放在 `plugins/<插件ID>/`，必须包含 `config.yaml` 与 `plugin.py`。插件名称、作者、版本和功能说明分别来自 `define.name`、`define.author`、`define.version`、`define.info`。管理台根据参数声明显示配置表单。

市场从本仓库 `index.json` 读取目录；不会读取 GitHub Token 管理页面。任何令牌、API Key、用户配置、聊天记录或运行缓存均不得上传。

```text
soulbot_plugins/
├── README.md
├── PLUGIN_SPEC.md
├── index.json
└── plugins/
    └── service_host/
        ├── config.yaml
        ├── plugin.py
        ├── README.md
        └── LICENSE
```

本仓库只发布源代码，不包含上游程序、模型文件或运行数据。安装、启用和权限授权分开执行；刷新市场目录不会执行 Python 代码。
