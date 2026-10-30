# SoulBot 插件开发规范 v1

文档日期：2026-10-30。本文是插件加载器与插件市场的实现契约；执行端仍在开发，实际支持状态以 SoulBot 已发布版本为准。

## 1. 单文件夹包

一个插件对应一个完整目录 `plugins/<id>/`。`id` 只允许小写字母开头的小写字母、数字和下划线，长度 1—40；安装后不得以路径或网址代替 ID。

必需文件为 `config.yaml` 与 `plugin.py`。发布包还应包含用途说明 `README.md` 和明确许可证 `LICENSE`；可以在同一目录包含 Python 辅助模块及必要静态资源。禁止符号链接、绝对路径、路径穿越、虚拟环境、`.git`、缓存、可执行安装脚本和私人数据。

运行时安装目录为 SoulBot 实例的 `plugins/<id>/`。移入目录后扫描可识别元数据；默认不执行代码，启用操作由管理员明确触发。不自动安装插件声明的任意 Python 依赖。

## 2. config.yaml

使用 UTF-8 和安全 YAML；配置文件最大 64 KiB，不使用自定义 YAML 标签或重复键。

```yaml
api_version: 1

define:
  name: 示例问候
  author: ikun441
  version: v0.1.0
  info: 根据管理员配置生成一条问候文本，供机器人作为工具结果使用。

parameters:
  greeting:
    type: string
    label: 问候前缀
    description: 模型调用此工具时使用的固定前缀。
    default: 你好
    max_length: 120
  repeat:
    type: integer
    label: 重复次数
    default: 1
    minimum: 1
    maximum: 3
  enabled_suffix:
    type: boolean
    label: 添加结束语
    default: true
  style:
    type: enum
    label: 风格
    default: friendly
    options: [friendly, plain]
  api_key:
    type: string
    label: 可选外部服务密钥
    default: ""
    secret: true
    max_length: 4096

config:
  greeting: 你好
  repeat: 1
  enabled_suffix: true
  style: friendly
```

`define` 的四个字段全部必需，且不能包含密钥或私人身份信息：

| 字段 | 含义 | 限制 |
| --- | --- | --- |
| `define.name` | 管理台显示名称 | 1—80 字 |
| `define.author` | 作者名称 | 1—80 字 |
| `define.version` | 插件版本 | `vX.Y.Z`，不使用浮点数 |
| `define.info` | 功能及用途说明 | 1—1000 字，按纯文本显示 |

`api_version` 必须为整数 `1`。未知根字段和未知参数应被拒绝。

`parameters` 是表单 Schema，`config` 是非敏感默认值。参数名称使用小写字母开头的字母、数字和下划线，最多 40 字；每个插件最多 50 项。支持 `string`、`integer`、`number`、`boolean`、`enum`。数字必须有限，整数不能用布尔值代替；枚举值为非空字符串列表。边界字段用于服务端验证，不能只依赖浏览器。

`secret: true` 仅适用于字符串，默认值必须为空，不能出现在 `config` 中。普通读取接口只返回“是否已配置”，不返回密钥；留空保留已有密钥，清除须单独明确操作。运行时密钥保存在 SoulBot 私有存储，不修改公开插件包。普通参数的运行时覆盖值也与发布包分开保存，重启后保留。

插件通过 SDK 读取经过验证的当前参数；不得自行修改 `config.yaml`、SoulBot 主配置或其他插件配置。

## 3. Python 入口与 SDK

固定入口为 `plugin.py` 的同步 `register(api)`，不接受模型指定的模块、函数名或 Shell 命令。导入与注册只发生在启用时，不发生在列表扫描或参数预览时。

```python
from pydantic import BaseModel, ConfigDict, Field

class GreetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)

def register(api):
    async def greet(args, context):
        prefix = api.params["greeting"]
        return {"message": f"{prefix}，{args.name}。"}

    api.tool(
        name="greet",
        description="按已配置前缀生成一条问候文本。",
        schema=GreetArgs,
        handler=greet,
        permission="approval",
        timeout=10,
        max_calls=2,
    )
```

`api.params` 每次返回当前已验证参数的副本，包含仅在服务端可读的密钥值。工具内部名称为插件内的短 ID；运行时使用 `plugin_<插件ID>__<工具ID>` 隔离命名，不能覆盖内置工具或其他插件。

`schema` 为 Pydantic BaseModel 类型，`handler` 为异步函数，接收已验证参数与服务端创建的 ToolContext。`timeout` 为 1—120 秒，`max_calls` 为 1—5。工具返回有界、可 JSON 序列化的结构化数据，不返回任意路径、原始 HTML 或不可验证的平台成功声明。

`permission` 可声明 `deny`、`auto` 或 `approval`，默认 `approval`。最终执行策略由管理员控制，插件不能自行批准或把模型参数当作授权。首版仅提供工具注册，不提供任意消息事件订阅或后台自启动回调。

## 4. 生命周期、记忆与外发

市场安装后默认禁用。管理员确认来源并启用后才导入代码、注册工具；禁用后新调用立即拒绝，排队和执行前再次检查状态。保存参数不执行插件代码，也不自动向 QQ 发消息。

插件工具遵守 SoulBot 的参数验证、调用次数、超时、审计和审批流程。机器人操作前按设置检索局部与全局记忆，只使用已审核、获授权且未关闭的条目；检索结果不授予公开私人信息的权利。

工具结果与 QQ 实际发送分离，不将“生成成功”写成“平台已发送”。需要外发的能力应交给宿主执行器重新检查会话权限、冷却、免打扰和媒体类型。费用未知或超过预警阈值不因预算阻止调用，但必须正确标记和记账。

Python 插件在当前系统用户权限下执行。SDK 的工具权限并非系统级沙箱，不能阻止恶意 Python 代码直接访问操作系统；启用前须核对来源与许可证，不能宣称已实现操作系统隔离。

## 5. 远程市场索引

本仓库的 `index.json` 使用如下结构：

```json
{
  "format": 1,
  "plugins": [
    {
      "id": "example_greeting",
      "path": "plugins/example_greeting",
      "name": "示例问候",
      "author": "ikun441",
      "version": "v0.1.0",
      "info": "示例功能说明",
      "files": {
        "config.yaml": "此处填写文件的64位SHA256",
        "plugin.py": "此处填写文件的64位SHA256",
        "README.md": "此处填写文件的64位SHA256",
        "LICENSE": "此处填写文件的64位SHA256"
      }
    }
  ]
}
```

上述示意摘要不是有效散列，不能作为可安装目录发布。正式索引的全部 `files` 必须是实际文件 SHA-256；单插件最多 100 个文件、总大小 10 MiB，单文件不超过 2 MiB。

市场刷新先解析仓库分支到确定的 Commit SHA，再读取该提交的索引。安装时仅下载同一提交、同一插件目录内列出的相对文件，逐一校验摘要和 `config.yaml`，元数据须与索引一致。不得在下载过程中执行 Python；失败时清理暂存目录并保留已有插件。安装不覆盖已安装版本，更新须单独明确执行。

目录数据不包含认证信息。需要访问私有仓库时，只使用宿主服务端安全存储的令牌，不把令牌写进 URL、索引、日志或浏览器存储。`https://github.com/settings/tokens` 仅是凭据管理页面，不是插件索引。

## 6. 提交与验收

发布前应验证 YAML/参数边界、元数据完整性、版本格式、索引摘要、启用/禁用、错误参数、超时及拒绝/审批策略。普通参数和密钥的保存读取须分别测试；不能用空实现或伪造网络结果声明插件可用。

提交内容只包含插件源代码、元数据、说明、许可和索引。禁止上传 `.env`、真实密钥、登录 Cookie、用户参数覆盖值、聊天数据库、日志、缓存、虚拟环境与第三方模型文件。
