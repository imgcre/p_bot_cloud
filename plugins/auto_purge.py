import asyncio
from dataclasses import dataclass
from datetime import datetime
import time
import traceback
from nap_cat_types import GetGroupMemberInfoResp
from plugin import Context, Inject, InstrAttr, Plugin, any_instr, autorun, delegate, enable_backup, route
from mirai.models.entities import GroupMember

from typing import TYPE_CHECKING, Final, Optional

from utilities import AchvEnum, AchvOpts, GroupLocalStorage, get_logger
if TYPE_CHECKING:
    from plugins.known_groups import KnownGroups
    from plugins.achv import Achv
    from plugins.admin import Admin
    from plugins.nap_cat import NapCat

logger = get_logger()

class AutoPurgeAchv(AchvEnum):
    INACTIVE_MARK = 0, '潜水标记', '获得此标记三天内仍没有发言, 则将被清出群聊', AchvOpts(locked=True, hidden=True)
    ...

@dataclass
class AutoPurgeMan():
    last_speak_ts: Optional[float] = None

@route('auto_purge')
@enable_backup
class AutoPurge(Plugin):
    gls: GroupLocalStorage[AutoPurgeMan] = GroupLocalStorage[AutoPurgeMan]()

    known_groups: Inject['KnownGroups']
    achv: Inject['Achv']
    admin: Inject['Admin']
    nap_cat: Inject['NapCat']

    INACTIVE_NOTIFICATION_DAYS_THRESHOLD: Final = 7
    INACTIVE_REMOVE_DAYS_THRESHOLD: Final = 3
    
    # 连续7天没有发言的话丢到待清除名单中
    # 在待清除名单中连续3天，则踢出群

    @delegate()
    async def get_man(self, man: AutoPurgeMan):
        return man
    
    @delegate()
    async def get_last_active_ts(self, man: AutoPurgeMan, *, info: Optional[GetGroupMemberInfoResp] = None):
        if info is None:
            info = await self.nap_cat.get_group_member_info()
        latest_active_ts = max(info.join_time, info.last_sent_time)
        if man.last_speak_ts is not None:
            latest_active_ts = max(man.last_speak_ts, latest_active_ts)
        return latest_active_ts

    # TODO: SEND_TEMP_MSG
    # @autorun
    async def purge_process(self, ctx: Context):
        while True:
            await asyncio.sleep(1)
            with ctx:
                for group_id in self.known_groups:
                    resp = await self.bot.member_list(group_id)
                    group = await self.bot.get_group(group_id)

                    for i, member in enumerate(resp.data):
                        async with self.override(member):
                            try:
                                try:
                                    info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()
                                except:
                                    logger.error(f'无法获得{member.member_name}({member.id})的成员信息')
                                    continue
                                if info.title != '':
                                    await self.achv.remove(AutoPurgeAchv.INACTIVE_MARK, force=True)
                                    continue

                                latest_active_ts = await self.get_last_active_ts(info=info)

                                span = time.time() - latest_active_ts
                                span_days = int(span / 60 / 60 // 24)

                                if span_days <= self.INACTIVE_NOTIFICATION_DAYS_THRESHOLD:
                                    await self.achv.remove(AutoPurgeAchv.INACTIVE_MARK, force=True)
                                    continue

                                if await self.achv.has(AutoPurgeAchv.INACTIVE_MARK):
                                    # TODO: 超过三天, 移除群聊, 顺便删除INACTIVE_MARK
                                    obtained_ts = await self.achv.get_achv_obtained_ts(AutoPurgeAchv.INACTIVE_MARK)
                                    if time.time() - obtained_ts > 60 * 60 * 24 * self.INACTIVE_REMOVE_DAYS_THRESHOLD:
                                        name = await self.achv.get_raw_member_name()
                                        await self.bot.kick(group_id, member.id, '自动清理潜水群员, 误踢请重新加回')
                                        await self.achv.remove(AutoPurgeAchv.INACTIVE_MARK, force=True)
                                        await self.admin.boardcast_to_admins(mc=[f'自动清理了潜水成员"{name}"({member.id})'])
                                else:
                                    try:
                                        # await self.bot.send_temp_message(member.id, group_id, [
                                        #     f'您在群{group.get_name()}({group_id})已经有{span_days}天没有冒泡啦, '
                                        #     f'bot将在{self.INACTIVE_REMOVE_DAYS_THRESHOLD}天后执行自动清理潜水群员程序, '
                                        #     '在此期间内进行冒泡可避免被误踢, 如被误踢请重新加回'
                                        # ])
                                        await self.nap_cat.send_msg(text=f'您在群{group.get_name()}({group_id})已经有{span_days}天没有冒泡啦')
                                        logger.debug(f'[潜水通知] ({i + 1}/{len(resp.data)}) {member.get_name()}: {span_days}天')
                                        await self.achv.submit(AutoPurgeAchv.INACTIVE_MARK, silent=True)
                                        await asyncio.sleep(5)
                                        await self.nap_cat.send_msg(text=f'bot将在{self.INACTIVE_REMOVE_DAYS_THRESHOLD}天后执行自动清理潜水群员程序')
                                        await asyncio.sleep(5)
                                        await self.nap_cat.send_msg(text=f'在此期间内进行冒泡可避免被误踢, 如被误踢请重新加回')
                                    finally:
                                        await asyncio.sleep(5 * 60)
                            except:
                                traceback.print_exc()

            await asyncio.sleep(60 * 60)
    
    @any_instr(InstrAttr.FORCE_BACKUP)
    async def remove_inactive_mark(self, man: AutoPurgeMan):
        man.last_speak_ts = time.time()
        if await self.achv.has(AutoPurgeAchv.INACTIVE_MARK):
            await self.achv.remove(AutoPurgeAchv.INACTIVE_MARK, force=True)
            await self.nap_cat.send_msg(text=f'🎉检测到您已冒泡, 已重置潜水计时')