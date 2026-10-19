# 主动消息日程判断

在主动消息发送前，结合当前聊天上下文和 LivingMemory 中的日程，调用现有对话模型判断用户此刻是否忙碌。明确忙碌时跳过本轮，空闲或无法确定时继续原发送流程。

这是独立扩展插件，安装后默认关闭拦截。它不维护另一份日程数据库，不修改主动消息插件源码，也不向用户发送检查提示。

## 兼容性与依赖

已验证的组合：

| 组件 | 版本或范围 |
| --- | --- |
| AstrBot | 4.29.1；声明支持 `>=4.29.1,<5` |
| [主动消息 Proactive Chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) | v1.2.6 |
| [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | 2.7.0-beta.2 |
| 平台 | QQ 官方机器人、OneBot v11；仅配置列表内的私聊 |
| 模型 | AstrBot 中已配置、能够返回 JSON 的对话模型 |

必须先安装并启用上述两个插件；LivingMemory 需要可用的向量模型和已完成初始化的记忆引擎。Python 依赖见 `requirements.txt`，`sqlite3` 等其余依赖来自标准库。

目前使用了上述插件的事件及记忆引擎接口。其他版本尚未验证；升级后请先用模拟检查确认兼容性。正常情况下，每轮增加一次对话模型调用和两次记忆检索；跨过已识别的忙闲边界时会复核，产生额外调用费用。

## 安装与首次配置

1. 在 AstrBot 的插件页面选择“从链接安装”，填写本仓库地址：`https://github.com/Sun-XT9527/astrbot_plugin_schedule_guard`。
2. 打开本插件配置，将目标私聊的完整 UMO 填入“允许检查的私聊会话 UMO”。不要填写群聊或向外公开自己的真实 UMO。
3. 判断模型留空时使用当前会话的对话模型，也可指定已有模型。
4. 向 bot 提供文字日程，包括日期、星期、起止时间、例外和改期。图片课表不在本插件的处理范围内。
5. 打开管理页，点击“保存并验证已有文字日程”，确认找到完整文字日程并能够从 LivingMemory 召回。此操作通过 LivingMemory 的既有写入接口保存原文；相同原文尽量避免重复保存。
6. 分别模拟一个忙碌时刻和一个空闲时刻，检查判断及引用依据。确认后，在配置中开启“启用日程拦截”并保存。

UMO 可通过 AstrBot 的会话管理或现有主动消息配置获取。本仓库没有内置任何真实会话编号、日程、密钥或记忆数据库；测试采用合成资料。

## 管理页

进入“插件 → 主动消息日程判断 → Pages → dashboard”。页面提供模拟检查、初始日程保存与召回验证、检查记录和当前下一次任务时间。

若当前 AstrBot 的内嵌 Pages 空白，可以在已登录的同一浏览器打开：

```text
http://localhost:18841/api/plug/astrbot_plugin_schedule_guard/page/view
```

请按自己的部署地址和端口替换 `http://localhost:18841`。此入口沿用 AstrBot 原有登录验证，不需要另填 API 密钥。插件不注册聊天命令或供人格调用的工具。

## 执行规则

```text
主动触发 → 原有免打扰检查 → 读取上下文与日程记忆 → 独立判断
  busy    → 停止事件，跳过正文与 TTS，原插件安排下一轮
  free    → 继续原发送流程
  unknown → 继续原发送流程
```

- 每次显式调用 LivingMemory 的 `search_memories`，两组查询各取五条并按记忆 ID 去重，沿用其会话和人格隔离规则。
- 判断使用北京时间，覆盖日期范围、跨年、全天占用、停课和提前结束。最近明确的取消、改期优先于旧安排；助手猜测和虚拟人生日程不作为用户日程证据。
- 判断不使用人格提示词、不执行工具、不写入聊天记录。模型输出必须能引用输入中的用户消息或记忆原文，否则记为未知。
- 资料不足、任一组记忆检索失败、判断超时或格式错误均记为未知并放行。“没有找到日程”不代表已确认空闲。
- 忙碌跳过不会增加未回复计数，也不会额外向 QQ 提示。生成正文期间跨过已识别的日程边界时，在 TTS 和发送前重新判断。
- “已放行”与“确认已发送”分别记录；只有原发送事件的实际成功标记才确认发送。
- 关闭拦截开关或停用本插件后，恢复原主动消息行为。

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用实际拦截；关闭时仍可模拟 |
| `session_list` | `[]` | 允许检查的完整私聊 UMO 列表 |
| `provider_id` | 空 | 留空使用当前会话模型 |
| `judge_timeout_seconds` | `30` | 判断超时，运行时限制为 1–120 秒 |
| `memory_timeout_seconds` | `10` | 单次记忆检索超时，运行时限制为 1–30 秒 |

## 数据与隐私

日程只存入原有 LivingMemory。插件在 AstrBot 的插件数据目录中保存检查用的 SQLite 数据库，其中包含会话 ID、触发时间、判断理由、引用的日程片段、发送状态及已观察到的下一轮时间。日志也会记录理由和证据，请勿公开上传这些运行数据。

检查会将本轮上下文与召回记忆交给用户自行配置的对话模型；记忆检索使用 LivingMemory 已配置的向量服务。插件没有另设第三方接口或内置密钥。

## 开发与验证

在仓库根目录运行：

```bash
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

设置环境变量 `PROACTIVE_PLUGIN_ROOT`，指向已安装的主动消息 v1.2.6 插件目录，可以额外运行其真实执行流的隔离集成测试；未设置时该项会跳过。测试不调用真实模型或 QQ 发送接口。测试包按实际目录位置加载，不要求仓库克隆目录具有特定名称。

## 致谢与许可证

本插件通过 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的事件扩展接口接入，并对接 [Proactive Chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) 的主动事件和 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 的记忆接口。采用独立插件以便单独关闭、保留检查记录，并避免修改这两个依赖的源码；不是上述项目的官方组件。

本仓库代码采用 [MIT License](LICENSE)。依赖项目遵循各自的许可证。
