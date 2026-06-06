# NapCat 正向 WebSocket 迁移计划

## 目标

移除本项目对 Overflow/mirai 网络兼容层的依赖。本项目直接作为 OneBot 11 正向 WebSocket 客户端连接 NapCat 服务端：

```text
p_bot_cloud -> ws://127.0.0.1:3001 -> NapCat
```

`yiri-mirai` 只保留为本地消息/事件模型兼容包，不再使用 `Mirai`、`WebSocketAdapter` 或 mirai-console/Overflow 网络链路。

## 约束

- 必须使用正向 WS：本项目是客户端，NapCat 是服务端。
- 不引入完整 bot 框架，避免把现有插件系统迁移到 NoneBot/Koishi/NcatBot。
- 现有插件尽量保留 `GroupMessage`、`MessageChain`、`At`、`Image` 等类型。
- NapCat API token 不写死在插件中，优先从配置或环境变量读取。
- 本机执行 Python 使用 `uv`。

## 实施步骤

1. 新增轻量 NapCat/OneBot 适配层。
   - 维护一个 WebSocket 连接。
   - API 请求使用 `{action, params, echo}`。
   - 按 `echo` 匹配响应。
   - 无 `echo` 的消息按 OneBot 事件处理。

2. 新增本地 mirai 兼容补丁。
   - 在启动时补齐项目历史依赖的 `MarketFace`、`ShortVideo` 类型。
   - 避免继续手工修改 `.venv`。

3. 将 OneBot 事件转换为现有事件模型。
   - `message/group` -> `GroupMessage`
   - `message/private` -> `FriendMessage` 或 `TempMessage`
   - `notice/group_recall` -> `GroupRecallEvent`
   - `notice/notify + poke` -> `NudgeEvent`
   - `notice/group_increase` -> `MemberJoinEvent`
   - `notice/group_ban + lift_ban` -> `MemberUnmuteEvent`
   - `notice/group_card` -> `MemberCardChangeEvent`
   - `request/group` -> `MemberJoinRequestEvent`

4. 替换启动入口。
   - `app.py` 不再创建 `Mirai(... WebSocketAdapter(...))`。
   - 改用 `NapCatBot(config.BOT_QQ_ID, ws_url=...)`。
   - 保留原有 `bot.on(...)` 业务处理结构。

5. 迁移 bot API 方法。
   - 发送：`send_group_message`、`send_friend_message`、`send_temp_message`、`send`
   - 管理：`recall`、`mute`、`unmute`、`kick`、`member_admin`
   - 查询：`get_group`、`get_group_member`、`member_list`
   - 配置：`member_info().set`、`group_config().get/set`
   - 低频接口：群文件夹、群公告、合并转发按 NapCat API 映射。

6. 迁移 `plugins/nap_cat.py`。
   - 删除 HTTP `127.0.0.1:3000` 直连和硬编码 token。
   - 统一调用 `self.bot.call_action(...)`。

7. 验证。
   - `uv run python -m compileall ...`
   - 导入 `app.py` 不触发 Overflow/mirai 网络连接。
   - 全仓库不再存在 `Mirai(`、`WebSocketAdapter`、`MIRAI_HOST`、`MIRAI_PORT` 的运行时代码路径。

## 验收标准

- 启动项目只连接 `ws://127.0.0.1:3001`。
- 不再启动或依赖 Overflow/mirai-console。
- 插件可以继续收到群消息并响应命令。
- `plugins/nap_cat.py` 的 NapCat API 调用复用正向 WS。
- 代码中没有需要连接 Overflow 的旧启动路径。

