import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from io import BytesIO
import json
import random
import re
import time
from typing import Dict, Final, Optional, Union
from collections import Iterable
from itertools import groupby

from activator import SharpActivator
import configs.config as config
from event_types import EffectiveSpeechEvent, ViolationEvent
import aiohttp
from mirai import At, AtAll, Event, Face, GroupMessage, Image, MessageChain, MessageEvent, Plain, TempMessage
from mirai.models.entities import GroupMember, MemberInfoModel, Group
from plugin import AchvCustomizer, Context, Inject, InstrAttr, MessageContext, PathArg, Plugin, any_instr, autorun, delegate, enable_backup, join_req_instr, joined_instr, recall_instr, route, top_instr
from utilities import AchvEnum, AchvExtra, AchvOpts, AchvRarity, AdminType, GroupLocalStorage, GroupOp, GroupSpec, ProxyContext, RewardEnum, Upgraded, get_logger, handler, throttle_config
from mirai.models.events import GroupRecallEvent, MemberJoinRequestEvent, MemberJoinEvent
import traceback
from mirai.models.api import RespOperate
from mirai.models.message import App, MusicShare, Quote, MarketFace, Source, Forward, ForwardMessageNode, ShortVideo, File
import cn2an
import os
import imagehash

import pyzbar.pyzbar
from pyzbar.pyzbar import ZBarSymbol
from PIL import Image as PImage

from pypinyin import lazy_pinyin

from nap_cat_types import GetGroupMemberInfoResp, GetStrangerInfoResp

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.events import Events
    from plugins.achv import Achv
    from plugins.fur import Fur
    from plugins.live import Live
    from plugins.reward import Reward
    from plugins.throttle import Throttle
    from plugins.nap_cat import NapCat
    from plugins.check_in import CheckIn

logger = get_logger()

class AdminAchv(AchvEnum):
    CAN_NOT_STOP = 0, '停不下来', '违规刷屏', AchvOpts(condition_hidden=True, custom_obtain_msg='刹车坏了，没法停下来', display='🥵', min_display_durtion=60 * 60 * 24 * 7)
    ORIGINAL_SIN = 1, '锁', '通过指令【#领取奖励】主动领取、猫德低于-3', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='由于猫德过低(<=-3), 获得成就【锁】, 对您新增了以下限制\n1. 更严格的刷屏判定: 连续发送五条消息将导致被禁言, 如果您的上一位发言者也有成就【锁】, 你们的刷屏计数将互相叠加(比如他发了三条消息, 而您在其中穿插着发了两条消息, 满足了连发五条消息的条件, 你们二者都将被禁言)\n2. 更长的禁言时长: 禁言时长变为普通群友的10倍\n\n您可在三天后自行通过指令【#解锁】来取消此惩罚成就', is_punish=True, prompt='拥有此成就的群成员将受到更严格的刷屏判定', display='🔒', display_weight=100, display_pinned=True)
    READY_FOR_PURGE = 2, '机票', '猫德达到或低于-8', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='警告: 您已多次违反规则', is_punish=True, prompt='拥有此成就的群成员将会在下次违规后被移出群', display='🎫', display_weight=99, display_pinned=True)
    WHITE_LIST = 3, '白名单', '管理员给予', AchvOpts(custom_obtain_msg='藏起了小尾巴', prompt='拥有此成就的群成员将不会受bot的违禁词系统管理')
    ALOOF = 4, '超然', '猫德低于-9', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='跳出了三界之外')
    DOGE = 5, '狗头保命', '发言中包含狗头, 成功抑制了一次违禁词检测'
    UNDERAGE = 6, '未成年', '由群主授予', AchvOpts(is_punish=True, display_pinned=True, display_priority=1),
    ALL_THE_TIME = 7, '一直都在', '没有特殊头衔的群友自动获得', AchvOpts(display='猫的', display_pinned=True, locked=True, hidden=True, display_priority=0)
    ENDLESS_REINCARNATION = 8, '无尽轮回', '每次重新进入聊天群触发成就进度+1', AchvOpts(rarity=AchvRarity.LEGEND, target_obtained_cnt=-1, unit='次', dynamic_name=True, display='📿')
    ADMIN = 9, '管理', '由群员通过【#申请管理】申请得到', AchvOpts(rarity=AchvRarity.LEGEND, display='🔰')
    UNSEEABLE = 10, '不可直视', '由群主授予', AchvOpts(locked=True, hidden=True)
    CURIOUS = 11, '好奇宝宝', '由群主授予', AchvOpts(locked=True, hidden=True)

# {"^[\\s哈呃。？\\?¿6]+?$": "[img:puzzled.gif]"}, 

class AdminReward(RewardEnum):
    SPECIAL_TITLE = 0, '头衔'
    ...

VIOLATE_THRESHOLD: Final[int] = 7
VIOLATE_THRESHOLD_ORIGINAL_SIN: Final[int] = 5

@dataclass
class BrushHistory():
    last_member_id: Optional[int] = None
    members_set: set[int] = field(default_factory=set)
    continuous_count: int = 0
    prev_has_orignial_sin: bool = False

    def next(self, member_ids: set[int], has_orignial_sin: bool):
        if has_orignial_sin and self.prev_has_orignial_sin:
            self.continuous_count += 1
            self.members_set.update(member_ids)
        else:
            
            if len(self.members_set.intersection(member_ids)) > 0:
                self.continuous_count += 1
            else:
                self.continuous_count = 1 

            # self.members_set = { member.id }
            
            if len(self.members_set.intersection(config.SUPER_ADMINS)) == 0:
                self.members_set = set(member_ids)
            else:
                self.members_set = set()

        self.prev_has_orignial_sin = has_orignial_sin
        
        logger.debug(f'{self.members_set=} {self.continuous_count=}')

    def clean_member_set(self):
        self.members_set = set()

    def is_violated(self):
        if len(self.members_set) == 0: return False
        if self.prev_has_orignial_sin:
            violate_threshold = VIOLATE_THRESHOLD_ORIGINAL_SIN
        else:
            violate_threshold = VIOLATE_THRESHOLD
        violate_threshold += len(self.members_set) - 1
        if self.continuous_count < violate_threshold: return False
        return True

@dataclass
class ViolationRecord():
    reason: str
    added_cnt: int
    created_ts: int = field(default_factory=time.time)

@dataclass
class ViolationMan(Upgraded):
    count: int = 0
    records: list[ViolationRecord] = field(default_factory=list)

    def append_record(self, record: ViolationRecord):
        self.records.append(record)

    def count_after_ts(self, ts: int):
        return len([r for r in self.records if r.created_ts >= ts])

@dataclass
class AdminOperationRecord():
    event: MessageEvent
    created_ts: int = field(default_factory=time.time)

@dataclass
class RequestedAdminMan():
    last_resign_ts: int = 0
    requested: bool = False
    operation_records: list[AdminOperationRecord] = field(default_factory=list)

    RESIGN_CD: Final[int] = 60 * 60 * 24 * 30 # 一个月

    def is_in_resign_cd(self):
        return time.time() - self.last_resign_ts < self.RESIGN_CD

    def request(self):
        self.requested = True
    
    def append_operation_records(self, r: AdminOperationRecord):
        self.operation_records.append(r)

@dataclass
class MemberAssociateMan():
    associated_menbers: list[set[int]] = field(default_factory=list)

    def associate(self, *member_ids: int):
        for ass in self.associated_menbers:
            if len(ass.intersection(member_ids)) > 0:
                ass.update(member_ids)
                break
        else:
            self.associated_menbers.append(set(member_ids))

    def disassociate(self, *member_ids: int):
        for ass in self.associated_menbers:
            ass.difference_update(member_ids)

        self.associated_menbers = [ass for ass in self.associated_menbers if len(ass) > 0]

    # readonly
    def get_associated(self, member_id: int):
        for ass in self.associated_menbers:
            if member_id in ass:
                return ass
        else:
            return {member_id}

@dataclass
class EffectiveSpeechRecord():
    created_ts: int = field(default_factory=time.time)

@dataclass
class EffectiveSpeechMan():
    records: list[EffectiveSpeechRecord] = field(default_factory=list)

    def record(self):
        self.records.append(EffectiveSpeechRecord())

    def count_after_ts(self, ts: int):
        return len([r for r in self.records if r.created_ts >= ts])

@dataclass
class HistoryItem():
    member: GroupMember
    message_chain: MessageChain

@dataclass
class MessageHistoryMan():
    history: list[HistoryItem] = field(default_factory=list)
    MAX_HISTORY_LEN: Final = 100

    def append(self, item: HistoryItem):
        self.history.append(item)
        if len(self.history) >= self.MAX_HISTORY_LEN:
            self.history.pop(0)
    
class ReslovedCensorSpeechQual(Enum):
    BASE = auto()
    ALL = auto()
    AT = auto()
    CURIOUS = auto()
    PINYIN = auto()

@dataclass
class ReslovedCensorSpeechKey():
    quals: Dict[ReslovedCensorSpeechQual, list[str]]
    reason: str
    
    @classmethod
    def from_expr(cls, expr: str):
        reason, *remains = expr.split(':')

        return cls(
            reason=reason,
            quals={ReslovedCensorSpeechQual[(its := r.split('.'))[0].upper()]: its[1:] for r in remains},
        )

@route('管理')
@enable_backup
class Admin(Plugin, AchvCustomizer):
    gls_violation: GroupLocalStorage[ViolationMan] = GroupLocalStorage[ViolationMan]()
    gls_requested_admin: GroupLocalStorage[RequestedAdminMan] = GroupLocalStorage[RequestedAdminMan]()
    gls_violation: GroupLocalStorage[ViolationMan] = GroupLocalStorage[ViolationMan]()
    gls_effective_speech: GroupLocalStorage[EffectiveSpeechMan] = GroupLocalStorage[EffectiveSpeechMan]()
    gspec_mam: GroupSpec[MemberAssociateMan] = GroupSpec[MemberAssociateMan]()
    gspec_message_history_man: GroupSpec[MessageHistoryMan] = GroupSpec[MessageHistoryMan]()
    last_auto_clean_all_violation_cnt_ts: int = 0

    events: Inject['Events']
    achv: Inject['Achv']
    live: Inject['Live']
    reward: Inject['Reward']
    throttle: Inject['Throttle']
    nap_cat: Inject['NapCat']
    check_in: Inject['CheckIn']

    VIOLATION_ORIGINAL_SIN_THRESHOLD: Final = 3
    VIOLATION_READY_FOR_PURGE_THRESHOLD: Final = 9

    def __init__(self) -> None:
        self.gspec = GroupSpec[BrushHistory]()
        self.recall_by_bot_msgs = set()
        self.custom_recall_resons: dict[int, str] = {}

    async def get_achv_name(self, e: 'AchvEnum', extra: Optional['AchvExtra']) -> str:
        if e is AdminAchv.ENDLESS_REINCARNATION:
            cnt = extra.obtained_cnt if extra is not None else 0
            return f'第{cn2an.an2cn(cnt + 1)}世'
        return e.aka

    @join_req_instr()
    async def auto_join(self, event: MemberJoinRequestEvent):
        
        return RespOperate.ALLOW, '自动通过入群申请'
        # man = self.gls_violation.get_data(event.group_id, event.from_id)
        # if man is not None:
        #     return RespOperate.ALLOW, '自动通过入群申请'

        # if self.live.is_living:
        #     profile = await self.bot.user_profile(event.from_id)
        #     if profile.level >= 16:
        #         return RespOperate.ALLOW, '自动通过入群申请'
        ...

    @joined_instr()
    async def handle_joined(self, event: MemberJoinEvent, member: GroupMember, man: Optional[ViolationMan], fur: Inject['Fur']):
        info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()
        
        # profile = await self.bot.member_profile(member.group.id, member.id)
        logger.debug(f'{info.qq_level=}')

        res = []

        inviter_has_purge = False
        if event.invitor is not None:
            async with self.override(event.invitor):
                inviter_has_purge = await self.achv.has(AdminAchv.READY_FOR_PURGE)

        if man is not None:
            await self.achv.submit(AdminAchv.ENDLESS_REINCARNATION)
            pic_res = await fur.deliver_light_bulb(factor=10)
            if pic_res is not None:
                res.extend(pic_res)

        if  not await self.achv.has(AdminAchv.READY_FOR_PURGE):
            if info.qq_level < 16:
                await self.achv.submit(AdminAchv.READY_FOR_PURGE, silent=True)
                res.extend(['由于您当前QQ等级过低, bot为您标记了【机票】, 期间若存在刷屏等违规行为, 将会被bot飞踢'])
            elif inviter_has_purge:
                await self.achv.submit(AdminAchv.READY_FOR_PURGE, silent=True)
                res.extend(['由于您的邀请人拥有【机票】, bot为您标记了【机票】, 期间若存在刷屏等违规行为, 将会被bot飞踢'])
            
        
        await self.achv.update_member_name()

        if len(res) > 0:
            return res

    @recall_instr()
    async def handle_recall_vio(self, event: GroupRecallEvent):
        if event.operator is None: return
        if event.author_id == event.operator.id: return
        if event.message_id in self.recall_by_bot_msgs: return

        # if event.operator.id == self.bot.qq: return
        logger.debug(f'{event.author_id=}, {event.operator.id=}, {event.message_id=}')
        reason = '被管理员撤回消息'
        if event.message_id in self.custom_recall_resons:
            reason = self.custom_recall_resons[event.message_id]
            self.custom_recall_resons.pop(event.message_id)
        await self.inc_violation_cnt(reason=reason, hint=reason)

    @delegate()
    async def make_mistakes(self, event: GroupMessage, *, to: int = 1):
        if to < 0: to = 1
        if event.sender.id not in config.SUPER_ADMINS:
            async with self.override(event.sender):
                await self.inc_violation_cnt(reason='主动犯错', to=to, hint=f'通过指令"#犯{to}次错"主动犯错')
        else:
            await self.inc_violation_cnt(reason='主动犯错', to=to, hint=f'通过指令"#犯{to}次错"主动犯错')

    @top_instr('.*?犯(?P<cnt>.+?)次错.*?')
    async def make_mistakes_multi_cmd(self, cnt: PathArg[str]):
        await self.make_mistakes(to=int(cn2an.cn2an(cnt, "smart")))

    @top_instr('.*?犯错.*?')
    async def make_mistakes_cmd(self):
         await self.make_mistakes()

    @delegate()
    async def get_morality(self, man: ViolationMan):
        morality = -man.count
        if morality < -100:
            return f'当前猫德 < -100'
        return f'当前猫德: {morality}'

    @top_instr('猫德')
    async def show_morality_cmd(self, man: ViolationMan):
        morality = -man.count

        tx = []

        if morality <= 0:
            tx.append('每次违规都会扣除1猫德，猫德过低会受到系统处罚。猫德大于255会获得一个传说级成就【晨钟暮鼓】')
            ...
        
        if self.live.is_living:
            tx.append('正在直播中，连续点击直播间画面中间每100次可增加1猫德，每场直播最多可增加4猫德左右（b站限制了每位观众的最高点赞量）\n\n')

        tx.append(await self.get_morality())

        return tx

    @top_instr('驱逐投票')
    async def expulsion_vote(self, at: At):
        ...

    @top_instr('解锁')
    async def unlock(self):
        if not await self.achv.has(AdminAchv.ORIGINAL_SIN):
            return '您当前不需要解锁'
        
        ts = await self.achv.get_achv_obtained_ts(AdminAchv.ORIGINAL_SIN)
        span = datetime.now().replace(tzinfo=None) - datetime.fromtimestamp(ts).replace(tzinfo=None)
        if span.days < 3:
            return f'还需要再等{3 - span.days}天才能解锁'

        await self.achv.remove(AdminAchv.ORIGINAL_SIN)
        return '恭喜您, 解锁成功'

    @handler
    @delegate(InstrAttr.FORCE_BACKUP)
    async def on_effective_speech(self, event: EffectiveSpeechEvent, man: EffectiveSpeechMan):
        man.record()

    @delegate()
    async def check_admin_privilege(self, member: GroupMember, *, type: AdminType):
        if type == AdminType.ACHV:
            if not await self.achv.has(AdminAchv.ADMIN):
                raise RuntimeError('无管理员权限')
            
            if not await self.achv.is_used(AdminAchv.ADMIN):
                raise RuntimeError('请先【#佩戴 管理】成就')
            
        if type == AdminType.SUPER:
            if member.id not in config.SUPER_ADMINS:
                raise RuntimeError('无超级管理员权限')

    @delegate(InstrAttr.FORCE_BACKUP)
    async def append_admin_op_record(self, event: MessageEvent, man: RequestedAdminMan):
        man.append_operation_records(AdminOperationRecord(event))

    def privilege(self, *, type=AdminType.ACHV):
        outer = self
        class Ctx():
            async def __aenter__(self):
                await outer.check_admin_privilege(type=type)
                ...

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is not None: return
                await outer.append_admin_op_record()
                ...
            ...
        ...

        return Ctx()
    
    @delegate()
    async def check_proxy(self, event: Event, proxy_context: Optional[ProxyContext], *, enable_required: bool = False, disable_required: bool = False):
        if enable_required == disable_required:
            raise RuntimeError('参数错误')
        
        if isinstance(event, GroupMessage) and event.sender.id in config.SUPER_ADMINS:
            return
        
        if disable_required and proxy_context is not None:
            raise RuntimeError('此指令无法代执行')
        if enable_required and proxy_context is None:
            raise RuntimeError('此指令只可代执行')
            ...

    
    def proxy(self, *, enable_required: bool = False, disable_required: bool = False):
        
        outer = self
        class Ctx():
            async def __aenter__(self):
                await outer.check_proxy(enable_required=enable_required, disable_required=disable_required)
                ...

            async def __aexit__(self, exc_type, exc, tb):
                ...
            ...
        ...

        return Ctx()

    @top_instr('申请管理')
    async def request_admin(
        self, 
        member: GroupMember, 
        es_man: Optional[EffectiveSpeechMan], 
        vi_man: Optional[ViolationMan],
        requested_admin_man: Optional[RequestedAdminMan]
    ):
        await self.check_proxy(disable_required=True)
        if await self.achv.has(AdminAchv.ADMIN):
            return '已成为管理员, 无需重复申请'

        @dataclass
        class ReqCheckListItem():
            desc: str
            passed: bool = False

        checklist: list[ReqCheckListItem] = []

        checklist.append(ReqCheckListItem(
            desc='过去一周有效发言数 >= 100',
            passed=(
                es_man is not None 
                and es_man.count_after_ts(time.time() - 60 * 60 * 24 * 7) >= 100
            )
        ))

        checklist.append(ReqCheckListItem(
            desc='过去一周没有违规',
            passed=(
                vi_man is None
                or vi_man.count_after_ts(time.time() - 60 * 60 * 24 * 7) == 0
            )
        ))

        info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()

        # info: MemberInfoModel = await self.bot.member_info(member.group.id, member.id).get()
        from plugins.live import LiveAchv
        checklist.append(ReqCheckListItem(
            desc='群等级 >= 80 或 拥有成就【舰长】',
            passed=(
                int(info.level) >= 80 
                or await self.achv.has(LiveAchv.CAPTAIN)
            )
        ))

        checklist.append(ReqCheckListItem(
            desc='距离上次卸任超过30天',
            passed=(
                requested_admin_man is None
                or not requested_admin_man.is_in_resign_cd()
            )
        ))

        all_passed = all([item.passed for item in checklist])

        if not all_passed:
            return [
                '申请失败, 存在未满足的条件:\n\n',
                '\n'.join([
                    f'{"✅" if item.passed else "❌"} {item.desc}' for item in checklist
                ])
            ]

        self.backup_man.set_dirty()
        
        requested_admin_man = self.gls_requested_admin.get_or_create_data(member.group.id, member.id)
        requested_admin_man.request()
        await self.achv.submit(AdminAchv.ADMIN)

        # 含有惩罚型成就

        # 过去一周有效发言数 >= 100
        # 过去一周没有违规
        # 群等级 >= 80 或者是舰长

        #权益：撤回、补签、AI CD减短、精华

        # 需要把管理的操作历史记录下来
        ...

    @top_instr('设精')
    async def set_essence(self, group: Group, quote: Quote):
        async with self.privilege():
            await self.nap_cat.set_essence_msg(quote.id)

    @top_instr('设置管理')
    async def set_admin(self, group: Group, at: At):
        async with self.privilege(type=AdminType.SUPER):
            await self.bot.member_admin(group.id, at.target, True)

    @top_instr('全体', InstrAttr.NO_ALERT_CALLER)
    async def at_all(self, event: GroupMessage):
        async with self.privilege():
            return [AtAll()]
        ...

    @top_instr('创建文件夹')
    async def create_dir(self, name: str, group: Group):
        async with self.privilege():
            await self.bot.file_mkdir("", group.id, name)
            return 'ok'

    @top_instr('公告')
    async def update_anno(self, event: GroupMessage):
        async with self.privilege():
            for c in event.message_chain:
                if isinstance(c, Quote):
                    await self.bot.anno_publish(
                        event.group.id,
                        f'{c.origin}',
                        show_edit_card=False,
                        show_popup=True,
                        require_confirmation=True,
                        # image_path=self.path.data.of_file('anno.png')
                    )
                    break
            else:
                return '未选择目标消息'
        # await self.bot.anno_publish(
        #     group.id,
        #     '\n'.join([
        #         '您好呀, 咱是bot, 欢迎来到聊天群"暗物质汤泉"'
        #     ]),
        #     send_to_new_member=True,
        #     pinned=True,
        #     show_edit_card=False,
        #     show_popup=True,
        #     require_confirmation=True,
        #     image_path=self.path.data.of_file('anno.png')
        # )
            
    @top_instr('设置头衔')
    async def admin_set_special_title(self, at: At, title :str, group: Group):
        async with self.privilege(type=AdminType.SUPER):
            print(f'{title=}')
            await self.bot.member_info().set(group.id, at.target, MemberInfoModel(
                special_title=title
            ))

    @top_instr('设置空头衔')
    async def admin_set_empty_special_title(self, at: At, title :str, group: Group):
        async with self.privilege(type=AdminType.SUPER):
            print(f'{title=}')
            await self.bot.member_info().set(group.id, at.target, MemberInfoModel(
                special_title=title
            ))

    # @any_instr()
    # async def keep_long_title(self, member: GroupMember):
    #     if member.id == 755188173:
    #         await self.bot.member_info().set(member.group.id, 755188173, MemberInfoModel(
    #             special_title='困了三天三夜三更半夜不停歇'
    #         ))

    @top_instr('头衔')
    async def set_special_title(self, title: Optional[str], member: GroupMember):
        from plugins.live import LiveAchv

        if not await self.achv.is_used(LiveAchv.CAPTAIN):
            info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()
            if info.title == '':
                return ['当前无法修改头衔']
            
            if title is not None:
                await self.reward.get_reward(AdminReward.SPECIAL_TITLE)

        if title is None:
            title = ''
        
        await self.bot.member_info().set(member.group.id, member.id, MemberInfoModel(
            special_title=title
        ))

    @top_instr('关联', InstrAttr.FORCE_BACKUP, InstrAttr.NO_ALERT_CALLER)
    async def associate_cmd(self, man: MemberAssociateMan, *ats: At):
        async with self.privilege(type=AdminType.SUPER):
            man.associate(*[at.target for at in ats])
            return 'ok'
    
    @top_instr('解除关联', InstrAttr.FORCE_BACKUP)
    async def disassociate_cmd(self, man: MemberAssociateMan, *ats: At):
        async with self.privilege(type=AdminType.SUPER):
            man.disassociate(*[at.target for at in ats])
            return 'ok'
    
    @top_instr('查看关联', InstrAttr.NO_ALERT_CALLER)
    async def get_associated_cmd(self, at: At):
        associated: set[int] = await self.get_associated(member_id=at.target)
        associated = list(associated)
        
        def intersperse(lst, item):
            result = [item] * (len(lst) * 2 - 1)
            result[0::2] = lst
            return result
        
        return intersperse([At(target=i) for i in associated], '\n')

    
    @delegate()
    async def get_associated(self, man: MemberAssociateMan, *, member_id: int):
        return man.get_associated(member_id)

    @top_instr('(?P<only>仅?)撤回')
    async def recall_cmd(self, group: Group, only: PathArg[bool], quote: Optional[Quote], m_id: Optional[int], custom_reason: Optional[str]):
        async with self.privilege():
            for _ in range(1):
                if quote is not None:
                    m_id = quote.id
                    break
                if m_id is not None:
                    break
            else:
                return '未选择目标消息'
            
            resp = await self.bot.message_from_id(m_id, group.id)
            mc = resp.data.message_chain
        
            if mc is not None:
                for comp in mc:
                    if isinstance(comp, Image):
                        img = await self.load_image(comp)
                        img_hash = imagehash.crop_resistant_hash(img)
                        img.convert('RGB').save(self.path.data['hashes'].of_file(f'{img_hash}.jpg'))
            
            if only:
                self.recall_by_bot_msgs.add(m_id)
            await self.bot.recall(m_id, group.id)
            if custom_reason is not None:
                self.custom_recall_resons[m_id] = custom_reason

    @any_instr(InstrAttr.FORCE_BACKUP)
    async def record_msg_history(self, event: GroupMessage, member: GroupMember, man: MessageHistoryMan):
        man.append(
            HistoryItem(
                member=member,
                message_chain=MessageChain([f'【{event.message_chain.message_id}】', *event.message_chain[1:]])
            )
        )

    @top_instr('消息记录')
    async def msg_history_cmd(self, man: MessageHistoryMan):
        async with self.privilege():
            return [
                Forward(node_list=[
                    ForwardMessageNode.create(
                        item.member, 
                        [c for c in item.message_chain[:] if isinstance(c, (Plain, Image, Face, MarketFace))]
                    ) for item in man.history
                ])
            ]

    @top_instr('取消禁言')
    async def unmute_target(self, group: Group, at: At):
        async with self.privilege(type=AdminType.SUPER):
            await self.bot.unmute(group.id, at.target)

    @top_instr('清(除|空)猫德', InstrAttr.NO_ALERT_CALLER)
    async def clean_violation_cnt_cmd(self, at: At):
        async with self.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                await self.clean_violation_cnt()
            return ['已将', at, ' 的猫德清零']

    @top_instr('领取奖励')
    async def get_award(self, m: GroupMember, man: Optional[ViolationMan]):
        span = datetime.now().replace(tzinfo=None) - m.join_timestamp.replace(tzinfo=None)
        if man is None and span.days <= 3:
            return '要入群3天后才可以领取奖励哦'
        
        if await self.achv.has(AdminAchv.ORIGINAL_SIN):
            return '已获得奖励, 无需重复领取'

        await self.achv.submit(AdminAchv.ORIGINAL_SIN)
        ...

    @any_instr()
    async def update_special_title_related_achv(self, member: GroupMember):
        info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()

        has_all_the_time = await self.achv.has(AdminAchv.ALL_THE_TIME)
        has_special_title = info.title != ''

        if not has_special_title and not has_all_the_time:
            await self.achv.submit(AdminAchv.ALL_THE_TIME, silent=True)

        if has_special_title and has_all_the_time:
            await self.achv.remove(AdminAchv.ALL_THE_TIME, force=True)

    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def proxy_execute(self, event: GroupMessage, quote: Quote, ctx: MessageContext):
        member_id = quote.sender_id

        for c in event.message_chain:
            if isinstance(c, Plain):
                m = re.search(r'【(.*?)】', c.text)
                if m is None: continue
                activator = SharpActivator()
                copied = event.copy()
                copied.message_chain = MessageChain([copied.message_chain[0], Plain(m.group(1))])
                chain = activator.check(copied)
                logger.debug(f'{c.text=}, {m.group(1)=}, {chain=}')
                if chain is None:
                    continue
                break
        else: return

        async with self.privilege():
            if member_id == self.bot.qq and event.sender.id not in config.SUPER_ADMINS:
                await self.inc_violation_cnt(reason='操纵bot', hint='操纵bot')

            member = await self.member_from(member_id=member_id)
            async with self.override(member, ProxyContext()):
                await ctx.exec_cmd(chain)


    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def brush_warning(self, history: BrushHistory, member: GroupMember, gop: GroupOp, fur: Inject['Fur']):
        try:
            has_orignial_sin = await self.achv.has(AdminAchv.ORIGINAL_SIN)
            history.next(await self.get_associated(member_id=member.id), has_orignial_sin)
            # print(f'{history.members_set=}')
            if history.is_violated():
                saved_mute_targets = history.members_set
                res = await fur.deliver_light_bulb(mute_targets=saved_mute_targets)
                await self.inc_violation_cnt(reason='连续刷屏', hint='连续刷屏')
                await self.achv.batch_submit(AdminAchv.CAN_NOT_STOP, member_ids=saved_mute_targets)
                history.clean_member_set()
                if history.prev_has_orignial_sin:
                    await gop.send([f'宝宝巴逝: 如果你也持有【锁】, 请先稍等其他群友发言完成后再发言'])
                    ...
                if res is not None:
                    return [*[At(target=target) for target in saved_mute_targets], *res]
        except:
            traceback.print_exc()

    @delegate()
    async def kick_target(self, member_id: int, gop: GroupOp):
        await gop.send(['由于违反规则, 将成员', At(target=member_id), ' 移除群聊'])

        associated = await self.get_associated(member_id=member_id)
        logger.debug(f'{associated=}')
        for mid in associated:
            await self.bot.mute(gop.group.id, mid, 60 * 60 * 24 * 30)
            await self.bot.kick(gop.group.id, mid, '由于违反规则被自动移除群聊')

    @delegate()
    async def clean_violation_cnt(self, man: Optional[ViolationMan]):
        if man is None:
            return
        self.backup_man.set_dirty()
        man.count = 0

    @delegate()
    async def get_admins(self):
        ...

    @delegate()
    async def dec_violation_cnt(self, member: GroupMember, man: ViolationMan, *, cnt: int=1, force: bool=False):
        if man.count > 0 or force:
            man.count -= cnt
            self.backup_man.set_dirty()

            await self.events.emit(ViolationEvent(
                member_id=member.id,
                hint='完成一次签到',
                count = man.count,
                dec = True
            ))

    @delegate(InstrAttr.FORCE_BACKUP)
    async def inc_violation_cnt(self, member: GroupMember, gop: GroupOp, man: ViolationMan, *, reason: str=None, to: int=1, hint: str=None):
        if  to > 0 and await self.achv.has(AdminAchv.READY_FOR_PURGE):
            await self.kick_target(member.id)
            return
        
        man.count += to
        man.append_record(ViolationRecord(
            reason=reason,
            added_cnt=to
        ))

        await self.events.emit(ViolationEvent(
            member_id=member.id,
            hint=hint,
            count = man.count,
            dec = to < 0
        ))
        
        if reason is not None:
            if not isinstance(reason, Iterable) or isinstance(reason, str):
                reason = [reason]
            # 触发违规计数
            plus = '+'if to <= 0 else ''
            
            live_hint = ''

            if to > 0 and not await self.check_in.is_checked_in_today():
                live_hint = '(每日【#签到】可增加猫德'

            if to > 0 and self.live.is_living:
                bind_hint = '在直播间将你的QQ号作为弹幕内容发送出去并' if not (await self.live.is_user_bound()) else ''
                live_hint = f'({bind_hint}在直播间连续点击画面可增加猫德'

            morality = await self.get_morality()

            await gop.send([
                At(target=member.id), 
                ' 由于', *reason, f', 猫德{plus}{-to}, {morality}{live_hint}'
            ])

        if to <= 0:
            return

        member_join_ts = datetime.timestamp(member.join_timestamp)
        original_sin_obtained_ts: Union[None, float] = await self.achv.get_achv_obtained_ts(AdminAchv.ORIGINAL_SIN)

        if man.count > self.VIOLATION_READY_FOR_PURGE_THRESHOLD and original_sin_obtained_ts is not None and original_sin_obtained_ts > member_join_ts:
            await self.achv.submit(AdminAchv.ALOOF)

        if man.count >= self.VIOLATION_ORIGINAL_SIN_THRESHOLD:
            await self.achv.submit(AdminAchv.ORIGINAL_SIN)

        if man.count >= self.VIOLATION_READY_FOR_PURGE_THRESHOLD:
            await self.achv.submit(AdminAchv.READY_FOR_PURGE)

    @delegate(InstrAttr.BACKGROUND)
    async def boardcast_to_admins(self, group: Group, *, mc: list):
        members = await self.bot.member_list(group.id)

        # TODO: SEND_TEMP_MSG
        # for m in members:
        #     async with self.override(m):
        #         if await self.achv.is_used(AdminAchv.ADMIN):
        #             try:
        #                 await self.bot.send_temp_message(m.id, m.group.id, [f'【管理消息】【{group.name}】\n', *mc])
        #                 await asyncio.sleep(3)
        #             except: ...
        ...

    # @autorun
    # async def auto_clean_all_violation_cnt(self, ctx: Context):
    #     while True:
    #         await asyncio.sleep(1)
    #         with ctx:
    #             tz = pytz.timezone('Asia/Shanghai')
    #             today = datetime.now(tz=tz)
    #             is_a_week_ago = time.time() - self.last_auto_clean_all_violation_cnt_ts > 60 * 60 * 24 * 3
    #             # print(f'{is_a_week_ago=}, {today.weekday()=}, {self.last_auto_clean_all_violation_cnt_ts=}')
    #             if today.weekday() >= 4 and today.hour >= 12 and is_a_week_ago:
    #                 for item_group in self.gls_violation.groups.values():
    #                     for man in item_group.values():
    #                         man.count = 0
    #                 self.last_auto_clean_all_violation_cnt_ts = time.time()
    #                 self.backup_man.set_dirty()
    #                 for group_id in self.gls_violation.groups.keys():
    #                     await self.bot.send_group_message(group_id, ['【猫德清空】猫德箱里空空如也。。。'])
            
        ...

    async def load_image(self, img: Image):
        async with aiohttp.ClientSession() as session:
            async with session.get(img.url) as resp:
                content_type = resp.headers.get('Content-Type')
                pimg: PImage.Image = PImage.open(BytesIO(await resp.read()))
        if content_type != 'image/gif':
            return pimg
        logger.debug('found gif')
        buffered = BytesIO()
        pimg.convert('RGB').save(buffered, format="JPEG")
        return PImage.open(buffered)

    async def breakdown_chain(self, chain, regex, cb, ctx=None):
        if ctx is None:
            ctx = {}
        new_chain = []
        if type(chain) is str:
            chain = [chain]
        for comp in chain:
            txt = None
            if isinstance(comp, str):
                txt = comp
            if isinstance(comp, Plain):
                txt = comp.text
            if txt is None:
                new_chain.append(comp)
                continue
            of_sp = re.split(regex, txt)
            for idx, s in enumerate(of_sp):
                if idx % 2 != 0:
                    s = await cb(s, ctx)
                if s is not None and s != '':
                    if type(s) is list:
                        new_chain.extend(s)
                    else:
                        new_chain.append(s)
        return new_chain

    def mark_recall_protected(self, msg_id: int):
        self.recall_by_bot_msgs.add(msg_id)

    @any_instr()
    async def censor_speech(self, event: GroupMessage, member: GroupMember):
        # print(f'{event.message_chain=}')

        info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()

        is_in_white_list = info.title != '' or await self.achv.has(AdminAchv.WHITE_LIST)

        # if member.special_title != '': return
        # if await self.achv.has(AdminAchv.WHITE_LIST): return

        prob = 1 # 触发违禁词检测的概率
        doge_cnt = 0

        for c in event.message_chain:
            if isinstance(c, Face) and c.face_id == 277:
                doge_cnt += 1
        
        if doge_cnt > 0:
            prob = 0.1 * doge_cnt
        
        MAX_DOGE_CNT = 20

        doge_protected = doge_cnt > 0 and doge_cnt < MAX_DOGE_CNT and random.random() > prob

        with open(self.path.data.of_file('censor_speech.json'), encoding='utf-8') as f:
            censor_speech_o: dict = json.load(f)

        with open(self.path.data.of_file('forbidden_market_face.json'), encoding='utf-8') as f:
            forbidden_market_face_o: dict[str, dict[str, int]] = json.load(f)

        image_hashes = [imagehash.hex_to_multihash(file_name.split('.')[0]) for file_name in os.listdir(self.path.data['hashes']) if not file_name.startswith('.')]

        url_regex = r'(https?:\/\/)((([0-9a-z]+\.)+[a-z]+)|(([0-9]{1,3}\.){3}[0-9]{1,3}))(:[0-9]+)?(\/[0-9a-z%/.\-_]*)?(\?[0-9a-z=&%_\-]*)?(\#[0-9a-z=&%_\-]*)?'
        url_pattern  = re.compile(url_regex)

        async def try_recall(reason: Union[str, list], hint: Optional[str] = None, *, only: bool=False):
            if hint is None:
                if isinstance(reason, str):
                    hint = reason
                else:
                    hint = '未知原因'
                ...
            if doge_protected:
                await self.achv.submit(AdminAchv.DOGE)
                return
            try:
                self.recall_by_bot_msgs.add(event.message_chain.message_id)
                await self.bot.recall(event.message_chain.message_id, event.group.id)
                def filter_msg_comp(c):
                    if isinstance(c, Source):
                        return False
                    return True
                def map_msg_comp(c):
                    if isinstance(c, (Plain, str, Image, Face)):
                        return c
                    if isinstance(c, MusicShare):
                        return f'音乐分享《{c.title}》--{c.summary}'
                    if isinstance(c, App):
                        return f'APP: {c.content}'
                    if isinstance(c, MarketFace):
                        face_name = '未知'
                        for faces in forbidden_market_face_o.values():
                            for name, face_id in faces.items():
                                if face_id == c.id:
                                    face_name = name
                        return f'表情{{{face_name}:{c.name}}}'
                    logger.debug(f'{c=}')
                    return f'{type(c)}'

                if not only:
                    await self.boardcast_to_admins(mc=[
                        f'撤回了"{member.member_name}"({member.id})的消息: \n', *[map_msg_comp(c) for c in event.message_chain if filter_msg_comp(c)]
                    ])

            
                # for ad in self.engine.get_context().admins:
                #     def map_msg_comp(c):
                #         if isinstance(c, (Plain, str, Image)):
                #             return c
                #         if isinstance(c, MusicShare):
                #             return f'音乐分享《{c.title}》--{c.summary}'
                #         if isinstance(c, App):
                #             return f'APP: {c.content}'
                #         print(f'{c=}')
                #         return f'{type(c)}'
                #         ...
                #     await self.bot.send_friend_message(ad, mc)
            except: 
                traceback.print_exc()
            if not only:
                await self.inc_violation_cnt(reason=reason, hint=hint)

        if doge_cnt >= MAX_DOGE_CNT:
            await try_recall('太多的狗头', '消息中包含太多的狗头表情包')
            return

        # True -> 干掉了
        async def check_text(txt: str):
            sorted_sorted = sorted(txt)
            txt_groups = groupby(sorted_sorted)

            if not is_in_white_list:
                for k, v in txt_groups:
                    if len(list(v)) >= 70 and k not in (' ', '\n'):
                        await try_recall(f'消息中包含太多的"{k}"')
                        return True

            for expr, words in censor_speech_o.items():
                key = ReslovedCensorSpeechKey.from_expr(expr)
                if ReslovedCensorSpeechQual.ALL not in key.quals and is_in_white_list:
                    continue

                if ReslovedCensorSpeechQual.AT in key.quals and member.id not in (int(a) for a in key.quals[ReslovedCensorSpeechQual.AT] if a.isdecimal()):
                    continue

                if ReslovedCensorSpeechQual.CURIOUS in key.quals and not await self.achv.has(AdminAchv.CURIOUS):
                    continue

                if ReslovedCensorSpeechQual.PINYIN in key.quals:
                    txt_pinyin = lazy_pinyin(txt)

                for w_item in words:
                    replacer = None
                    if isinstance(w_item, str):
                        kw = w_item
                    elif isinstance(w_item, dict):
                        kw, replacer = next(iter(w_item.items()))

                    forb_word = None

                    if ReslovedCensorSpeechQual.PINYIN in key.quals:
                        kw_pinyin = lazy_pinyin(kw)
                        found_idx = find_subsequence_start(kw_pinyin, txt_pinyin)
                        if found_idx != -1:
                            forb_word = txt[found_idx:found_idx+len(kw_pinyin)]
                    else:
                        m = re.search(kw, txt)
                        if m is not None:
                            forb_word = m.group(0)

                    if forb_word is not None:
                        try:
                            async def img_op(s, ctx):
                                img_path = self.path.data.of_file(s)
                                return Image(path=img_path)
                            suffix = f'(推荐使用"{replacer}")' if replacer is not None else ''
                            chain = await self.breakdown_chain(suffix, r'\[img:(.*?)\]', img_op)
                            await try_recall([key.reason, *chain], f'消息中包含违禁词"{forb_word}", 补充理由: {key.reason}')
                        except:
                            traceback.print_exc()
                        return True
            if url_pattern.search(txt) is not None and not is_in_white_list:
                await try_recall('消息中包含不明链接')
                return True
            return False
        
        live_img_sent = False

        quote_senders = set[int]()

        for c in event.message_chain:
            try:
                if isinstance(c, Plain):
                    if await check_text(c.text): return
                if isinstance(c, MusicShare):
                    if await check_text(c.title): return
                if isinstance(c, Quote):
                    if c.sender_id is not None:
                        quote_senders.add(c.sender_id)
                if isinstance(c, File):
                    print(f'File {c=}')
                    ...
                if isinstance(c, MarketFace):
                    for reason, faces in forbidden_market_face_o.items():
                        only = 'only' in reason
                        if c.id in faces.values():
                            await try_recall(reason, reason, only=only)
                            return
                if isinstance(c, Image):
                    img = await self.load_image(c)
                    print(f'Image {c=}')
                    if await self.achv.has(AdminAchv.UNSEEABLE):
                        if img.width >= 500 or img.height >= 500 or 'gxh.vip.qq.com' in c.image_id:
                            await try_recall('不可直视', only=True)
                            return
                    qrcodes = pyzbar.pyzbar.decode(img)
                    logger.debug(f'{qrcodes=}')
                    if len(qrcodes) > 0 and not is_in_white_list:
                        await try_recall('消息中包含不明二维码')
                        return
                    target_hash = imagehash.crop_resistant_hash(img)
                    for full_hash in image_hashes:
                        seg, dist = full_hash.hash_diff(target_hash)
                        print(f'{seg=}, {dist=}, {str(full_hash)}')
                        if seg > 0 and dist < 4 * seg:
                            await try_recall('不适宜的图片')
                            return
                    if not live_img_sent:
                        await self.live.try_show_image(img=c)
                        live_img_sent = True
                if isinstance(c, ShortVideo):
                    if await self.achv.has(AdminAchv.UNSEEABLE):
                        await try_recall('不可直视', only=True)
                if not is_in_white_list:
                    if isinstance(c, Face):
                        logger.debug(f'face {c.face_id=}, {c.name=}')
                        if c.face_id in (
                            1, #撇嘴
                            14, #微笑
                            19, #吐
                            59, #便便
                            182, #笑哭
                        ):
                            await try_recall('使用了不友善的表情')
                            return
                    if isinstance(c, App):
                        await try_recall('消息中包含不明链接')
                        return  
                    if isinstance(c, At):
                        target: GroupMember = await self.member_from(at=c)
                        if target is not None:
                            async with self.override(target):
                                info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()
                            # span = datetime.now().replace(tzinfo=None) - target.last_speak_timestamp.replace(tzinfo=None)
                            if c.target not in quote_senders:
                                if time.time() - info.last_sent_time > 60 * 60 * 24 * 3:
                                    await try_recall('@潜水成员')
                                    return
                                if c.target in config.SUPER_ADMINS and time.time() - info.last_sent_time > 60 * 60:
                                    await try_recall('吵猫睡觉(@猫并且猫在最近一个小时内没有发言)')
                                    return 
            except: 
                traceback.print_exc()

    @any_instr()
    @throttle_config(name='提醒做作业', max_cooldown_duration=1*60*60, silent=True)
    async def alert_underage(self, _: GroupMessage):
        if not await self.achv.has(AdminAchv.UNDERAGE):
            return
        
        async with self.throttle as passed:
            if not passed: return

        return '作业写完了没'

def find_subsequence_start(sub, lst):
    """
    查找sub是否是lst的连续子序列，如果是则返回起始下标
    
    Args:
        sub: 子序列列表
        lst: 主列表
        
    Returns:
        int: 如果是连续子序列返回起始下标，否则返回-1
    """
    if not sub:  # 空列表是任何列表的子序列，返回第一个位置
        return 0
    if len(sub) > len(lst):
        return -1
    
    n, m = len(lst), len(sub)
    
    # 遍历主列表
    for i in range(n - m + 1):
        found = True
        # 检查连续m个元素是否匹配
        for j in range(m):
            if lst[i + j] != sub[j]:
                found = False
                break
        if found:
            return i
    return -1