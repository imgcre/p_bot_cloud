import traceback
from activator import SharpActivator
import mirai_compat  # noqa: F401
from mirai import Event, MessageChain, MessageEvent, Plain
import plugin
from plugin import CommandNotFoundError
from mirai.models.events import MemberCardChangeEvent, GroupRecallEvent, NudgeEvent, MemberJoinRequestEvent, MemberJoinEvent, MemberUnmuteEvent, GroupMessage, FriendMessage, StrangerMessage, TempMessage
from mirai.models.api import RespOperate
import zhconv
import configs.config as config
from napcat_adapter import NapCatBot, get_config_value

bot = NapCatBot(
    config.BOT_QQ_ID,
    ws_url=get_config_value(config, "NAPCAT_WS_URL", "ws://127.0.0.1:3001"),
    access_token=get_config_value(config, "NAPCAT_ACCESS_TOKEN", None),
)

activator = SharpActivator()

engine = plugin.Engine(bot)

@bot.on(MemberJoinRequestEvent)
async def on_join_req(event: MemberJoinRequestEvent):
    if event.group_id != 139825481:
        return
    with engine.of(event) as ctx:
        async def resp(op: RespOperate, msg='bot自动处理'):
            event_id = getattr(event, 'onebot_flag', event.event_id)
            await bot.resp_member_join_request_event(event_id, event.from_id, event.group_id, op, msg)
        await ctx.exec_join(resp)

@bot.on(Event)
async def on_event(event: Event):
    if isinstance(event, (MemberCardChangeEvent, GroupRecallEvent, MemberJoinEvent, MemberUnmuteEvent, NudgeEvent)):
        if isinstance(event, MemberCardChangeEvent):
            if event.member.group.id != 139825481:
                return
        if isinstance(event, GroupRecallEvent):
            if event.group.id != 139825481:
                return
        if isinstance(event, MemberJoinEvent):
            if event.member.group.id != 139825481:
                return
        if isinstance(event, MemberUnmuteEvent):
            if event.member.group.id != 139825481:
                return
        if isinstance(event, NudgeEvent):
            if event.subject.kind != 'Group' or event.subject.id != 139825481:
                return
        with engine.of(event) as ctx:
            await ctx.exec()
@bot.on(MessageEvent)
async def on_message(event: MessageEvent):
    if isinstance(event, GroupMessage):
        if event.group.id != 139825481:
            return
    if isinstance(event, FriendMessage):
        return
    if isinstance(event, StrangerMessage):
        return
    if isinstance(event, TempMessage):
        if event.group.id != 139825481:
            return
    with engine.of(event) as ctx:
        def map_text(comp):
            if isinstance(comp, Plain):
                t = comp.text
                t = t.replace('‭', '')
                t = zhconv.convert(t, 'zh-cn')
                return Plain(t)
            return comp

        event.message_chain = MessageChain([map_text(c) for c in event.message_chain])

        await ctx.exec_any(event.message_chain)

        chain = activator.check(event)
        if chain is None: 
            await ctx.exec_fall(event.message_chain)
            return

        try:
            await ctx.exec_cmd(chain)
        except CommandNotFoundError as e:
            traceback.print_exc()
            try:
                await ctx.exec_cmd(['notfound', *chain])
                # await ctx.exec_cmd(['ai', *chain])
            except: ...
            ...
        except Exception as e:
            # raise
            traceback.print_exc()
            await ctx.send()

# async def test_endpoint(request: Request):
#     # 获取 JSON 数据
#     data: dict[str, str] = await request.json()
    
#     # 或者手动解析
#     # body = await request.body()
#     # if body:
#     #     data = json.loads(body)
    
#     # 获取特定字段
#     name = data.get("name", "未知")
    
#     return JSONResponse({
#         "status": "success",
#         "name": name
#     })

def main():
    engine.load()

    bot.run(host='0.0.0.0')
    

if __name__ == '__main__':
    main()
