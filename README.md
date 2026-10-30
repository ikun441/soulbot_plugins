# SoulBot 插件仓库

用于 SoulBot 的单文件夹插件与市场索引。插件开发契约见 [PLUGIN_SPEC.md](PLUGIN_SPEC.md)。

## 当前状态

先发布开发规范与索引格式。SoulBot 插件加载器、侧边栏和配置界面正在开发；当前索引为空，不把尚未验收的插件列为可安装。

每个插件放在 `plugins/<插件ID>/`，必须包含 `config.yaml` 与 `plugin.py`。插件名称、作者、版本和功能说明分别来自 `define.name`、`define.author`、`define.version`、`define.info`。管理台根据参数声明显示配置表单。

市场从本仓库 `index.json` 读取目录；不会读取 GitHub Token 管理页面。任何令牌、API Key、用户配置、聊天记录或运行缓存均不得上传。

```text
soulbot_plugins/
├── README.md
├── PLUGIN_SPEC.md
├── index.json
└── plugins/
    └── example_greeting/
        ├── config.yaml
        ├── plugin.py
        ├── README.md
        └── LICENSE
```

目录示例用于说明结构，当前尚未发布示例包。安装、启用和权限授权分开执行；刷新市场目录不会执行 Python 代码。
