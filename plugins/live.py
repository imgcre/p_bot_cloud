import asyncio
from dataclasses import dataclass, field, asdict
from decimal import Decimal
import os
import random
import re
import string
import time
import traceback
import typing

import cn2an
import configs.config as config
from event_types import AchvRemovedEvent, ViolationEvent
from plugin import AchvCustomizer, Inject, Plugin, any_instr, autorun, delegate, enable_backup, InstrAttr, route, timer, top_instr
from aiomqtt import Client
import aiomqtt
from mirai import At, GroupMessage, Image
from mirai.models.entities import GroupMember, Group, GroupConfigModel
from bilibili_api import live, search
import json
from enum import Enum, auto
import mirai.models.message
import inflection
import aiohttp
import humanize
from PIL import Image as PImage, ImageOps
import pyzbar.pyzbar
import math

from typing import TYPE_CHECKING, Awaitable, Callable, ClassVar, Final, Optional, overload

from utilities import VOUCHER_NAME, VOUCHER_UNIT, AchvEnum, AchvExtra, AchvInfo, AchvOpts, AchvRarity, AdminType, GroupLocalStorage, RecallItem, Source, Upgraded, User, UserSpec, VoucherRecordChestNotifyTmpEnable, VoucherRecordExtraLiveAnswerExplanation, VoucherRecordExtraLiveCdkey, VoucherRecordExtraLiveGift, VoucherRecordExtraLiveGuard, breakdown_chain_sync, deserialize, get_delta_time_str, get_logger, handler, throttle_config, to_unbind

if TYPE_CHECKING:
    from plugins.achv import Achv
    from plugins.admin import Admin
    from plugins.known_groups import KnownGroups
    from plugins.throttle import Throttle
    from plugins.nap_cat import NapCat
    from plugins.voucher import Voucher, VoucherRecord
    from plugins.recall_scheduler import RecallScheduler
    from plugins.events import Events
    from plugins.check_in import CheckIn

logger = get_logger()

# 题库:
# dr400: https://chinaexam11-1251537182.cos.ap-shanghai.myqcloud.com/%E4%B8%93%E9%A1%B9%E9%A2%98%E5%BA%93%E8%B5%84%E6%96%99/8.%E5%9B%BE%E5%BD%A2%E6%8E%A8%E7%90%86400%E9%A2%98%EF%BC%88%E7%AD%94%E6%A1%88%2B%E8%AF%A6%E7%BB%86%E8%A7%A3%E6%9E%90%EF%BC%8C%E5%85%B1121%E9%A1%B5%EF%BC%89.pdf
# dr1000: https://www.docin.com/p-1924758414.html
# py1000: https://www.sanfoundry.com/1000-python-questions-answers/

# MCQ with answer

# BIND_HINT = '【#绑定账号】'
BIND_HINT = '请先在直播间将你的QQ号作为弹幕内容发送出去'

class LiveAchv(AchvEnum):
    CAPTAIN = 0, '舰长', '通过【#绑定账号】与B站账号相关联后并且是B站账号为纳延的舰长时自动获取', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='成为了猫咪的舰长', display='⚓', locked=True, dynamic_obtained=True)
    MIRROR_PAIR = 1, '镜像存在', '通过【#绑定账号】与B站账号相关联后获得', AchvOpts(rarity=AchvRarity.EPIC, custom_obtain_msg='完成了连结', display='💠', locked=True, dynamic_obtained=True, dynamic_name=True)
    CIVIL_SERVANT = 2, '公务员', '答对100次题目', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='上岸了', target_obtained_cnt=100, display='💼')
    ESCAPED = 3, '生生不息', '在打地鼠游戏中成功存活', AchvOpts(rarity=AchvRarity.EPIC, custom_obtain_msg='成功存活', display='🪀')
    MOKUGYO = 4, '晨钟暮鼓', '在直播间点赞达到25500次', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='木鱼声响彻天地', display='🐋', target_obtained_cnt=255, unit='声')
    FULLY_CHARGED = 5, '满格', '成功触发100次电力传输', AchvOpts(
        rarity=AchvRarity.LEGEND, 
        custom_obtain_msg='完全充满了', 
        display='🔋', 
        target_obtained_cnt=100, 
        unit='%'
    )
    HUNDRED_HOURS = 6, '常驻人口', '在直播间观看100小时', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='和猫咪一起度过了漫长时光', display='🌌', locked=True, target_obtained_cnt=100, unit='小时', custom_progress_str=True)

async def get_uid_by_name(uname: str):
    return (await search.search_by_type(uname, search_type=search.SearchObjectType.USER))['result'][0]['mid']

class BindState(): ...

@dataclass
class BindStateNotBind(BindState): ...

@dataclass
class BindStateWaitOpenId(BindState):
    from_group_id: int
    confirm_code: str = field(init=False)

    def __post_init__(self):
        self.confirm_code = ''.join(random.choices(string.ascii_lowercase, k=6))

@dataclass
class Guard():
    last_reward_ts: float = 0

    def should_grant_reward(self):
        month_in_seconds = 30 * 24 * 60 * 60
        return time.time() - self.last_reward_ts > month_in_seconds
    
    def mark_granted(self):
        if math.isclose(self.last_reward_ts, 0):
            self.last_reward_ts = time.time()
        else:
            self.last_reward_ts += 30 * 24 * 60 * 60

@dataclass
class BindStateBound(BindState, Upgraded):
    openid: str = None
    uname: str = None
    guard: Optional[Guard] = None
    uid: Optional[int] = None

    def is_guard(self):
        return self.guard is not None

@dataclass
class BindStateWaitConfirm(BindState):
    openid: str = None
    uname: str = None
    two_f_code: str = field(init=False)

    def __post_init__(self):
        self.two_f_code = random.choice('12345789')

@dataclass
class UserBindInfo():
    bind_state: BindState = BindStateNotBind()
    def start_bind(self, from_group_id: int) -> str:
        self.bind_state = BindStateWaitOpenId(from_group_id=from_group_id)
        return self.bind_state.confirm_code
    
    def check_confirm_code(self, confirm_code: str):
        return isinstance(self.bind_state, BindStateWaitOpenId) and self.bind_state.confirm_code == confirm_code

    def end_bind(self, openid: str, uname: str):
        if not isinstance(self.bind_state, BindStateWaitOpenId):
            raise RuntimeError('state error')
        prev_state = self.bind_state
        self.bind_state = BindStateWaitConfirm(openid=openid, uname=uname)
        return prev_state.from_group_id, self.bind_state.two_f_code
    
    async def confirm(self):
        if not isinstance(self.bind_state, BindStateWaitConfirm):
            raise RuntimeError('state error')
        prev_state = self.bind_state
        uid = None
        try:
            uid = await get_uid_by_name(prev_state.uname)
        except: ...
        self.bind_state = BindStateBound(openid=prev_state.openid, uname=prev_state.uname, uid=uid)

    async def direct_bind(self, openid: str, uname: str):
        uid = None
        try:
            uid = await get_uid_by_name(uname)
        except: ...
        self.bind_state = BindStateBound(openid=openid, uname=uname, uid=uid)
    
    def unbind(self):
        self.bind_state = BindStateNotBind()

    def is_bound(self):
        return isinstance(self.bind_state, BindStateBound)
    
    def get_bound(self) -> Optional['BindStateBound']:
        if isinstance(self.bind_state, BindStateBound):
            return self.bind_state
        else:
            return None
    
    def get_openid(self):
        if not isinstance(self.bind_state, BindStateBound): return
        return self.bind_state.openid
    
    def check_open_id(self, openid: str):
        if not isinstance(self.bind_state, BindStateBound): return False
        return self.bind_state.openid ==openid
    
    def get_uname(self):
        if not isinstance(self.bind_state, BindStateBound): return
        return self.bind_state.uname

@dataclass
class CaptainMan():
    last_welcom_ts: int = 0

    WELCOME_INTERVAL: Final[int] = 60 * 60 * 8

    def is_need_welcome(self):
        return time.time() - self.last_welcom_ts > self.WELCOME_INTERVAL
    
    def set_welcomed(self):
        self.last_welcom_ts = time.time()

@dataclass
class Pending():
    source: Source
    expired_ts: Optional[float]

@dataclass
class WhacAMolePending():
    source: Source
    created_ts: float = field(default_factory=time.time)

class QuestionState():
    @staticmethod
    def create(meta: list[str], source: Source):
        if meta[0] == 'sc':
            return QuestionStateSingleChoice.create(meta, source)
        raise RuntimeError('未知的题型')
    
    def get_prompt(self):
        return []

@dataclass
class QuestionStateIdle(QuestionState):
    ...

@dataclass
class QuestionStateSingleChoice(QuestionState):
    meta: list[str]
    source: Source
    recall_item: RecallItem = field(default_factory=RecallItem.dummy)
    created_ts: float = field(default_factory=time.time)

    @staticmethod
    def create(meta: list[str], source: Source):
        return QuestionStateSingleChoice(meta=meta, source=source)

    def get_prompt(self):
        return ['单选题, 请回复A、B、C、D']
    
    def get_question_name(self):
        return self.meta[1]
    
    def get_answer(self):
        return self.meta[2]

    def get_result(self, text: str):
        if text.upper() not in ('A', 'B', 'C', 'D'): return
        return AnswerResult.CORRECT if text.upper() == self.get_answer().upper() else AnswerResult.WRONG
    
    def is_expired(self):
        return time.time() - self.created_ts > 3 * 60

class AnswerResult(Enum):
    CORRECT = auto()
    WRONG = auto()

@dataclass
class QuestionStateAnswerExplanationConfirmation(QuestionState):
    explanation: str
    recall_item: RecallItem = field(default_factory=RecallItem.dummy)
    created_ts: float = field(default_factory=time.time)

    def should_show(self, text: str):
        return 'y' in text.lower()

    def is_expired(self):
        return time.time() - self.created_ts > 1 * 60
    ...

@dataclass
class WhacAMoleMan():
    question_state: QuestionState = field(default_factory=QuestionStateIdle)

    def to_idle_state(self):
        self.question_state = QuestionStateIdle()

    def update_state_by_question_file_name(self, question_file_name: str, source: Source):
        question_meta = question_file_name.split('.')
        self.question_state = QuestionState.create(question_meta, source)

class MusicState(): ...

@dataclass
class MusicStateIdle(MusicState):
    ...

@dataclass
class MusicStateSelect(MusicState, Upgraded):
    id: str
    created_ts: float = field(default_factory=time.time)

@dataclass
class MusicMan():
    state: MusicState = field(default_factory=MusicStateIdle)
    ...

class RPCFunc():
    ...

@dataclass
class RPCOptions():
    timeout: int=10
    pending: bool = False
    pending_expired_ts: Optional[float] = None

    def pending_expired_after(self, duration: float):
        self.pending = True
        self.pending_expired_ts = time.time() + duration
        return self

RPCOptionsFactory = Callable[[], RPCOptions]

@dataclass
class AddMusic(RPCFunc):
    query: str
    openid: str
    uname: str
    avatar: str

    __opts_factory__: ClassVar[RPCOptionsFactory] = lambda: RPCOptions(timeout=60).pending_expired_after(60)

    @dataclass
    class Response:
        succeed: bool
        reason: str

        def __post_init__ (self):
            if not self.succeed:
                raise RuntimeError(self.reason)

@dataclass
class Playlist(RPCFunc):
    @dataclass
    class Response:
        @dataclass
        class QueueItem():
            uname: str
            music_name: str

        queue: list[QueueItem]

@dataclass
class ScreenRecord(RPCFunc):
    __opts_factory__: ClassVar[RPCOptionsFactory] = lambda: RPCOptions().pending_expired_after(60)

    @dataclass
    class Response:
        succeed: bool
        reason: str

        def __post_init__ (self):
            if not self.succeed:
                raise RuntimeError(self.reason)
        ...

@dataclass
class WhacAMole(RPCFunc):
    openid: str
    avatar: str

    __opts_factory__: ClassVar[RPCOptionsFactory] = lambda: RPCOptions().pending_expired_after(60)

    @dataclass
    class Response:
        position: int

        def __post_init__ (self):
            if self.position < 0:
                if self.position == -2:
                    raise RuntimeError('请先观看直播')
                else:
                    raise RuntimeError('坑位已满')

@dataclass
class FetchMusicWaitTime(RPCFunc):
    openid: str

    @dataclass
    class Response:
        in_queue: bool
        min_duration: float = 0
        max_duration: float = 0

@dataclass
class UserSwitchInstrOnly(RPCFunc):
    openid: str
    __opts_factory__: ClassVar[RPCOptionsFactory] = lambda: RPCOptions().pending_expired_after(60)

    @dataclass
    class Response:
        succeed: bool
        reason: str

        def __post_init__ (self):
            if not self.succeed:
                raise RuntimeError(self.reason)

@dataclass
class RedeemCdkey(RPCFunc):
    openid: str
    cdkey: str
    __opts_factory__: ClassVar[RPCOptionsFactory] = lambda: RPCOptions().pending_expired_after(60)

    @dataclass
    class Response:
        succeed: bool
        reason: str
        count: int

        def __post_init__ (self):
            if not self.succeed:
                raise RuntimeError(self.reason)
        ...

@dataclass
class UnboundAccountCache():
    total_price: int = 0
    total_merit: int = 0

@dataclass
class HeatReacord():
    end_ts: float
    created_ts: float = field(default_factory=time.time)
    ...

@dataclass
class LiveStat():
    created_ts: float = field(default_factory=time.time)
    last_feed_ts: float = field(default_factory=time.time)
    heat_records: list[HeatReacord] = field(default_factory=list)
    chest_notify_user_ids: set[int] = field(default_factory=set)
    prev_online_openids: list[str] = field(default_factory=list)
    prev_online_openid_ts: Optional[float] = None

    def feed(self):
        self.last_feed_ts = time.time()

    def is_timeout(self):
        return time.time() - self.last_feed_ts > 120
    
    def build_digest(self):
        return LiveDigest(
            duration=time.time() - self.created_ts
        )

    def add_heat_record(self, end_ts: float):
        self.heat_records.append(HeatReacord(
            end_ts=end_ts
        ))

    def is_heating(self):
        return any((time.time() < rec.end_ts for rec in reversed(self.heat_records)))
    
@dataclass
class LiveDigest():
    duration: float

    @property
    def formatted_duration(self):
        hours, remainder = divmod(self.duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        return '{:02}时{:02}分{:02}秒'.format(int(hours), int(minutes), int(seconds))

@dataclass
class GlobalCaptainMan():
    captain_names: list[str] # 舰长的b站用户名

    ...

@route('live')
@enable_backup
class Live(Plugin, AchvCustomizer):
    user_binds: UserSpec[UserBindInfo] = UserSpec[UserBindInfo]()
    gls_captain: GroupLocalStorage[CaptainMan] = GroupLocalStorage[CaptainMan]()
    gls_whac_a_mole: GroupLocalStorage[WhacAMoleMan] = GroupLocalStorage[WhacAMoleMan]()
    gls_music: GroupLocalStorage[MusicMan] = GroupLocalStorage[MusicMan]()
    whac_a_mole_pendings: dict[str, WhacAMolePending] = {}
    pendings: dict[str, Pending] = {}
    unbound_account_caches: dict[str, UnboundAccountCache] = {}

    achv: Inject['Achv']
    admin: Inject['Admin']
    known_groups: Inject['KnownGroups']
    throttle: Inject['Throttle']
    nap_cat: Inject['NapCat']
    voucher: Inject['Voucher']
    recall_scheduler: Inject['RecallScheduler']
    events: Inject['Events']
    check_in: Inject['CheckIn']
    

    def __init__(self) -> None:
        self.mqtt_client = None
        # cmd_name, req_id
        self.rpc_queue: dict[str, dict[str, asyncio.Future]] = {}
        self.live_stat = None
        self.ts_last_effect_set = 0
        self.ts_last_screenshot = 0

        # self.whac_a_mole_pendings: dict[str, WhacAMolePending] = {}
        ...

    @property
    def is_living(self):
        return self.live_stat != None
        ...

    @delegate()
    async def is_user_bound(self, info: Optional[UserBindInfo]):
        return info is not None and info.is_bound()
    
    @delegate()
    async def get_associated_name(self, info: Optional[UserBindInfo]):
        if info is None:
            return None
        return info.get_uname()
    
    @delegate()
    async def is_guard(self, info: Optional[UserBindInfo]):
        if info is None:
            return False
        
        state = info.get_bound()

        if state is None:
            return False
        
        return state.is_guard()

    async def is_achv_obtained(self, e: 'AchvEnum'):
        if e is LiveAchv.MIRROR_PAIR:
            return await self.is_user_bound()
        if e is LiveAchv.CAPTAIN:
            return await self.is_guard()
        return False
    
    async def get_progress_str(self, e: 'AchvEnum', extra: 'AchvExtra') -> str:
        if e is LiveAchv.HUNDRED_HOURS:
            return f'{math.floor(extra.user_data / 3600)}小时{math.floor(extra.user_data / 60 % 60):02d}分(累计毛啵观看时长), 目标是100小时'
    
    @delegate()
    async def get_mirror_pair_name(self, info: Optional[UserBindInfo]):
        if info is not None and info.is_bound():
            return info.get_uname()

    async def get_achv_name(self, e: 'AchvEnum', extra: Optional['AchvExtra']) -> str:
        if e is LiveAchv.MIRROR_PAIR:
            name = await self.get_mirror_pair_name()
            if name is not None:
                return f'绑:{name}'
        return e.aka

    @handler
    @delegate()
    async def on_achv_removed(self, event: AchvRemovedEvent):
        if event.e is LiveAchv.MOKUGYO:
            await self.admin.inc_violation_cnt(to=LiveAchv.MOKUGYO.opts.target_obtained_cnt, reason='木鱼没声了', hint='撤回了成就"晨钟暮鼓 "')

    @handler
    @delegate()
    async def on_violation(self, event: ViolationEvent):
        if event.count < 0:
            await self.achv.submit(LiveAchv.MOKUGYO, override_obtain_cnt=-event.count)
        else:
            await self.achv.remove(LiveAchv.MOKUGYO, force=True, notify=False)

    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def auto_welcome_captain(self, member: GroupMember):
        if not await self.achv.is_used(LiveAchv.CAPTAIN):
            return
        
        man = self.gls_captain.get_or_create_data(member.group.id, member.id)
        if man.is_need_welcome():
            self.backup_man.set_dirty()
            man.set_welcomed()
            return [Image(path=self.path.data.of_file('captain.gif'))]

    @delegate()
    async def handle_message(self, message: aiomqtt.Message):
        logger.debug(f'{message.topic=}')
        if message.topic.matches('/live/status/started') and self.live_stat is None:
            self.live_stat = LiveStat()

            for group_id in self.known_groups:
                group = await self.bot.get_group(group_id)
                async with self.override(group):
                    await self.update_group_name_based_on_live_status()

            # room = live.LiveRoom(config.BILIBILI_LIVEROOM_ID)
            # room_info = (await room.get_room_info())['room_info']
            # title = room_info['title']
            # cover_img_url = room_info['cover']
            # room_id = room_info['room_id']

            # for group_id in self.known_groups:
            #     group = await self.bot.get_group(group_id)
            #     async with self.override(group):
            #         await self.update_group_name_based_on_live_status()
            #     # TODO
            #     text1 = '\n'.join([
            #         '啵啦啵啦',
            #     ])

            #     # 🛠️✨
            #     text2 = '\n'.join([
            #         'changelog: ',
            #         '🛠️修复了老观众无法点歌的bug',
            #         '✨老观众在开播后可以先点几首歌',
            #     ])

            #     text3 = '\n'.join([
            #         "目前可以公开的情报:",
            #         '#踩我: 在直播间生成一只地鼠',
            #         '#多久到我: 查询点歌的排队时长',
            #         f'#宝箱提醒: 开启👉本次👈毛啵的宝箱生成提醒(消耗0.1{VOUCHER_UNIT}{VOUCHER_NAME})',
            #         '#切换伴奏',
            #     ])

            #     texts = [text1, text2, text3]

            #     for text in texts:
            #         await self.bot.send_group_message(group_id, [
            #             text
            #         ])
            #         await asyncio.sleep(2)
                # await self.bot.anno_publish(
                #     group_id,
                #     '\n'.join([
                #         f'📢小猫咪偷偷开播啦!',
                #         title,
                #         f'https://live.bilibili.com/{room_id}',
                #         '目前可以用的指令:',
                #         '#截屏',
                #         '#录屏（动图，需要久等',
                #         '#点歌',
                #         '#点歌队列',
                #     ]),
                #     send_to_new_member=True,
                #     pinned=True,
                #     show_edit_card=False,
                #     show_popup=True,
                #     require_confirmation=True,
                #     image_url=cover_img_url
                # )
        if message.topic.matches('/live/status/stopped'):
            await self.on_live_stopped()
        if message.topic.matches('/live/resp/+'):
            j = json.loads(message.payload)
            req_id = j['id']
            cmd_name = message.topic.value.split('/')[-1]
            if cmd_name in self.rpc_queue:
                cmd_sepc_queue = self.rpc_queue[cmd_name]
                if req_id in cmd_sepc_queue:
                    cmd_sepc_queue[req_id].set_result(j)
        if message.topic.matches('/live/event/bind'):
            j = json.loads(message.payload)
            # openid uname confirm_code
            confirm_code = j['confirm_code']
            openid = j['openid']
            uname = j['uname']
            found_item = next((item for item in self.user_binds.users.items() if item[1].check_confirm_code(confirm_code)), None)
            if found_item is None:
                return
            qq_id, user_bind_info = found_item
            from_group_id, two_f_code = user_bind_info.end_bind(openid, uname)
            await self.bot.send_group_message(from_group_id, [
                At(target=qq_id),
                f' 确认与"{uname}"绑定吗? 回复{two_f_code}确认, 回复n取消'
            ])
            self.backup_man.set_dirty()
        if message.topic.matches('/live/event/bind_direct'):
            j = json.loads(message.payload)
            qq_str = j['qq'] # str
            openid = j['openid']
            uname = j['uname']

            qq_id = int(qq_str)

            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
            if found_item is not None: # 对应的openid已经与某个用户绑定了
                already_qq, _ = found_item
                logger.warning(f"该b站账号已经和其他qq({already_qq})绑定了")
                return

            members: list[GroupMember] = []

            for group_id in self.known_groups:
                member = await self.bot.get_group_member(group_id, qq_id)
                if member is not None:
                    members.append(member)

            if len(members) == 0:
                logger.warning(f"没找到这个人")
                return # 没找到这个人
            
            user_bind_info = self.user_binds.get_or_create_data(qq_id)
            if user_bind_info.is_bound():
                logger.warning(f"该qq号已经绑定了其他b站账号")
                return
            
            await user_bind_info.direct_bind(openid, uname)
            self.backup_man.set_dirty()

            logger.info(f"已执行绑定")

            async with self.override(User(qq_id)):
                text = await self.take_lost()

            for member in members:
                await self.bot.send_group_message(member.group.id, [
                    At(target=member.id), f' ', *text
                ])
        # if message.topic.matches('/live/event/guard'):
        #     j = json.loads(message.payload)
        #     openid = j['openid']
        #     price = j['price']
        #     found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)

        #     if found_item is not None:
        #         await self.apply_price(found_item, price, captain=True)
        #     else:
        #         self.inc_cache_price(openid, price)
        if message.topic.matches('/live/event/whac_a_mole_slot_failed'):
            j = json.loads(message.payload)
            pending = self.try_pop_pending_by(req_id=j['id'])
            if pending is None: return
            async with self.override(pending.source.member):
                await self.achv.submit(LiveAchv.ESCAPED, silent=True)
            await pending.source.op.send(['[新成就]由 ', At(target=pending.source.member.id), ' 生成的地鼠活到了最后: 加时1分钟'])
        if message.topic.matches('/live/event/whac_a_mole_slot_succeed'):
            j = json.loads(message.payload)
            pending = self.try_pop_pending_by(req_id=j['id'])
            if pending is None: return
            await pending.source.op.send(['由 ', At(target=pending.source.member.id), ' 生成的地鼠被踩了: 减时5秒'])
        if message.topic.matches('/live/event/music_candidate_list'):
            j = json.loads(message.payload)
            req_id = j['id']
            if req_id not in self.pendings: return
            pending = self.pendings[req_id]
            async with self.override(pending.source.member):
                await self.set_music_select_state(id=req_id)
            await pending.source.op.send([
                '请 ', 
                At(target=pending.source.member.id), 
                ' 选择序号(回复0取消):\n', 
                '\n'.join([f'{i + 1}: 《{s["name"]}》 -{s["author"]}' for i, s in enumerate(j['songs'])]),
                *(['*没有可供选择的歌曲*'] if len(j['songs']) == 0 else []),
            ])
        if message.topic.matches('/live/event/screen_record_done'):
            j = json.loads(message.payload)
            pending = self.try_pop_pending_by(req_id=j['id'])
            if pending is None: return
            url = j['url']
            async with aiohttp.ClientSession() as session:
                async with session.head(url) as resp:
                    length = int(resp.headers.get('Content-Length'))
            await pending.source.op.send([
                f'录屏GIF上传中...({humanize.naturalsize(length, gnu=True)})'
            ])
            await pending.source.op.send([
                Image(url=url)
            ])
        if message.topic.matches('/live/event/gift'):
            print('!!!gift step 1')
            j = json.loads(message.payload)
            openid = j['openid']
            price = j['price']

            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)

            if found_item is not None:
                print('!!!gift item found')
                await self.apply_price(found_item, price)
            else:
                self.inc_cache_price(openid, price)
        if message.topic.matches('/live/event/merit'):
            j = json.loads(message.payload)
            openid = j['openid']
            count = j['count']

            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)

            if found_item is not None:
                await self.apply_merit(found_item, count)
            else:
                self.inc_cache_merit(openid, count)
        if message.topic.matches('/live/event/compensate'):
            j = json.loads(message.payload)
            price: int = j['price']
            reason: str = j['reason']
            openids: list[str] = j['openids']

            ats: list[At] = []

            for openid in openids:
                found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
                if found_item is not None:
                    qq_id, _ = found_item
                    await self.apply_price(found_item, price)
                    ats.append(At(target=qq_id))
                else:
                    self.inc_cache_price(openid, price)

            if len(ats) == 0:
                return
            
            ext_text = '各' if len(ats) > 1 else ''

            for group_id in self.known_groups:
                await self.bot.send_group_message(group_id, [
                    f'由于{reason}, 向',
                    *ats,
                    f' {ext_text}补偿了{VOUCHER_NAME} x {self.price_to_voucher_count(price)}'
                ])
        if message.topic.matches('/live/event/chest_opened'):
            ...
            j = json.loads(message.payload)
            openid = j['openid']
            price = j['price']
            price *= 10 # 10倍

            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)

            if found_item is not None:
                await self.apply_price(found_item, price)

                qq_id, _ = found_item
                for group_id in self.known_groups:
                    member = await self.bot.get_group_member(group_id, qq_id)
                    if member is not None:
                        await self.bot.send_group_message(group_id, [
                            At(target=member.id), f' 打开了宝箱，获得了{VOUCHER_NAME} x {self.price_to_voucher_count(price)}'
                        ])
            else:
                self.inc_cache_price(openid, price)
        if message.topic.matches('/live/event/chest_generated'):
            j = json.loads(message.payload)
            openid = j['openid']
            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
            if found_item is not None and self.live_stat is not None:
                qq_id, _ = found_item
                if qq_id not in self.live_stat.chest_notify_user_ids:
                    return
                for group_id in self.known_groups:
                    member = await self.bot.get_group_member(group_id, qq_id)
                    if member is not None:
                        await self.bot.send_group_message(group_id, [
                            At(target=member.id), f' 大人您在猫窝中有一个宝箱待解锁'
                        ])
        if message.topic.matches('/live/event/feed'):
            if self.live_stat is not None:
                self.live_stat.feed()
        if message.topic.matches('/live/event/heating_end_timestamp_changed'):
            j = json.loads(message.payload)
            value = j['value']
            print(f'{value=}')
            if self.live_stat is not None:
                self.live_stat.add_heat_record(value)
        if message.topic.matches('/live/event/bot_music_on_demand_playing'):
            if not self.is_living:
                return
            
            j = json.loads(message.payload)
            openid = j['openid']
            music_name = j['music_name']
            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
            if found_item is None:
                return
            
            qq_id, _ = found_item
            for group_id in self.known_groups:
                member = await self.bot.get_group_member(group_id, qq_id)
                if member is None: continue
                await self.bot.send_group_message(group_id, [
                    '正在播放由', At(target=member.id), f'点播的《{music_name}》。你现在可以使用【#切换伴奏】将正在播放的歌曲切换至伴奏版'
                ])
        if message.topic.matches('/live/event/all_red_packet_claimed'):
            for group_id in self.known_groups:
                await self.bot.send_group_message(group_id, [
                    '兑换码已经都被兑换光了！'
                ])
        if message.topic.matches('/live/event/rest_red_packet'):
            j = json.loads(message.payload)
            price: int = j['price']

            member = await self.bot.get_group_member(139825481, 755188173)
            if member is None:
                print(f'rest_red_packet {member is None=}')
                return

            async with self.override(member):
                record: 'VoucherRecord' = await self.voucher.adjust(
                    cnt=Decimal(price) / 100,
                    extra=VoucherRecordExtraLiveCdkey()
                )

            for group_id in self.known_groups:
                await self.bot.send_group_message(group_id, [
                    At(target=member.id),
                    f' 获得了剩下还没被兑换的{record.count}{VOUCHER_UNIT}{VOUCHER_NAME}',
                ])

        if message.topic.matches('/live/event/online_audiences'):
            if not self.is_living:
                return
            
            j = json.loads(message.payload)
            openids: list[str] = j['openids']

            openid_qqid_map: dict[str, int] = {}

            for openid in openids:
                found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
                if found_item is not None:
                    qq_id, _ = found_item
                    openid_qqid_map[openid] = qq_id

            for group_id in self.known_groups:
                will_check_in_members: list[GroupMember] = []

                for qq_id in openid_qqid_map.values():
                    member = await self.bot.get_group_member(group_id, qq_id)
                    if member is not None:
                        will_check_in_members.append(member)

                if len(will_check_in_members) == 0:
                    continue
                
                checked_members = await self.check_in.batch_check_in(members=will_check_in_members)
                if len(checked_members) == 0:
                    continue

                await self.bot.send_group_message(group_id, [
                    *[At(target=member.id) for member in checked_members],
                    f' 正在观看猫播, 已自动签到'
                ])

            await self.update_audience_online_time(openids)
                    
        if message.topic.matches('/live/event/coming_music'):
            if not self.is_living:
                return
            
            j = json.loads(message.payload)
            openid = j['openid']

            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
            if found_item is None:
                return

            qq_id, _ = found_item
            for group_id in self.known_groups:
                member = await self.bot.get_group_member(group_id, qq_id)
                if member is None: continue
                await self.bot.send_group_message(group_id, [
                    At(target=member.id), f' 你有一首歌曲即将开始播放, 排到时未进入直播间将自动取消播放'
                ])
        if message.topic.matches('/live/event/started'):
            j = json.loads(message.payload)
            changelog = j['changelog']

            for group_id in self.known_groups:
                texts = []

                texts.append('\n'.join([
                    '啵啦啵啦！',
                ]))

                if len(changelog) > 0:
                    texts.append('\n'.join([
                        '本次更新: ',
                        *changelog
                    ]))
                    
                texts.append('\n'.join([
                    "目前可以公开的情报:",
                    '#踩我: 在直播间生成一只地鼠',
                    '#多久到我: 查询点歌的排队时长',
                    f'#宝箱提醒: 开启👉本次👈毛啵的宝箱生成提醒(消耗0.1{VOUCHER_UNIT}{VOUCHER_NAME})',
                    '#切换伴奏',
                ]))

                for text in texts:
                    await self.bot.send_group_message(group_id, [
                        text
                    ])
                    await asyncio.sleep(2)

    def inc_cache_price(self, openid: str, price: int):
        if openid not in self.unbound_account_caches:
            self.unbound_account_caches[openid] = UnboundAccountCache()
        cache = self.unbound_account_caches[openid]
        cache.total_price += price
        self.backup_man.set_dirty()
        print(f'cache price {openid=}, {price=}')

    def inc_cache_merit(self, openid: str, count: int):
        if openid not in self.unbound_account_caches:
            self.unbound_account_caches[openid] = UnboundAccountCache()
        cache = self.unbound_account_caches[openid]
        cache.total_merit += count
        self.backup_man.set_dirty()

    async def apply_price(self, found_item: tuple[int, UserBindInfo], price: int, *, captain: bool=False, scale: int = 10):
        # if price <= 0: return
        qq_id, _ = found_item
        user = User(qq_id)
        async with self.override(user):
            if captain:
                ...
                # await self.achv.submit(LiveAchv.CAPTAIN)
            print('!!! gift will return feeding')
            return await self.return_voucher_for_gift_feeding(price, scale=scale)

    async def apply_merit(self, found_item: tuple[int, UserBindInfo], count: int):
        if count <= 0: return
        qq_id, _ = found_item
        for group_id in self.known_groups:
            member = await self.bot.get_group_member(group_id, qq_id)
            if member is None: continue
            async with self.override(member):
                await self.admin.inc_violation_cnt(to=-count, reason='敲木鱼', hint='在直播间点赞')

    @delegate(InstrAttr.FORCE_BACKUP)
    async def set_music_select_state(self, man: MusicMan, *, id: str):
        man.state = MusicStateSelect(id=id)

    async def screenshoot(self):
        resp = await self.rpc('screenshoot')
        return resp['url']

    async def set_effect(self, name: str):
        await self.rpc('set_effect', {
            'name': name
        })

    @delegate()
    async def whac_a_mole(self, source: Source, *, openid: str, avatar: str):
        m_req_id: Optional[str] = None
        async def req_id_cb(req_id: str):
            nonlocal m_req_id
            m_req_id = req_id
            self.whac_a_mole_pendings[req_id] = WhacAMolePending(source)
        try:
            resp = await self.rpc('whac_a_mole', {
                'openid': openid,
                'avatar': avatar
            }, req_id_cb=req_id_cb)
            if resp['position'] < 0:
                if resp['position'] == -2:
                    raise RuntimeError('请先观看直播')
                else:
                    raise RuntimeError('坑位已满')
            return resp['position']
        except:
            if m_req_id is not None:
                self.whac_a_mole_pendings.pop(m_req_id)
            raise

    def try_pop_pending_by(self, *, req_id: str):
        if req_id in self.pendings:
            pending = self.pendings[req_id]
            self.pendings.pop(req_id)
            self.backup_man.set_dirty()
            return pending

    @overload
    async def x(self, func: AddMusic) -> AddMusic.Response: ...

    @overload
    async def x(self, func: WhacAMole) -> WhacAMole.Response: ...

    @overload
    async def x(self, func: Playlist) -> Playlist.Response: ...

    @overload
    async def x(self, func: ScreenRecord) -> ScreenRecord.Response: ...

    @overload
    async def x(self, func: FetchMusicWaitTime) -> FetchMusicWaitTime.Response: ...

    @overload
    async def x(self, func: UserSwitchInstrOnly) -> UserSwitchInstrOnly.Response: ...

    @overload
    async def x(self, func: RedeemCdkey) -> RedeemCdkey.Response: ...

    @delegate()
    async def x(self, func: RPCFunc, source: Source, *, opts: Optional[RPCOptions]=None):
        m_req_id: Optional[str] = None

        print('before opt')
        if opts is None:
            __opts_factory__ = getattr(func, '__opts_factory__', None)
            if __opts_factory__ is not None:
                opts = to_unbind(__opts_factory__)()
            else:
                opts = RPCOptions()

        print('before underscore')
        name = inflection.underscore(func.__class__.__name__)
        data = asdict(func)
        print(f'call {name=}, {data=}, {func.Response=}')
        async def req_id_cb(req_id: str):
            nonlocal m_req_id
            m_req_id = req_id
            if opts.pending:
                self.pendings[req_id] = Pending(source=source, expired_ts=opts.pending_expired_ts)
                self.backup_man.set_dirty()
        try:
            res = await self.rpc(name, data, timeout=opts.timeout, req_id_cb=req_id_cb)
        except:
            self.try_pop_pending_by(req_id=m_req_id)
            raise
        print(f'{res=}')
        try:
            return deserialize(func.Response, res)
        except:
            traceback.print_exc()
            raise
            ...

    async def rpc(self, name, data: dict = None, *, timeout: int=10, req_id_cb: Callable[[str], Awaitable[None]]=None) -> dict:
        if data is None:
            data = {}
        while True:
            req_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
            
            if name not in self.rpc_queue:
                self.rpc_queue[name] = {}

            if req_id in self.rpc_queue[name]:
                continue

            data['id'] = req_id
            break

        future = asyncio.Future()
        if req_id_cb is not None:
            await req_id_cb(req_id)
        self.rpc_queue[name][req_id] = future

        await self.mqtt_client.publish(f'/live/req/{name}', json.dumps(data))

        try:
            await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError('服务未响应')
        finally:
            self.rpc_queue[name].pop(req_id)
        
        return future.result()
    
    async def update_audience_online_time(self, openids: list[str]):
        if not self.is_living:
            return
        
        curr_time = time.time()

        openid_qqid_map: dict[str, int] = {}

        for openid in openids:
            found_item = next((item for item in self.user_binds.users.items() if item[1].check_open_id(openid)), None)
            if found_item is not None:
                qq_id, _ = found_item
                openid_qqid_map[openid] = qq_id

        will_update_progress_qqids: list[int] = []
        timespan = None

        if self.live_stat.prev_online_openid_ts is not None:
            timespan = curr_time - self.live_stat.prev_online_openid_ts
            for openid in openid_qqid_map.keys():
                if openid in openids and openid in self.live_stat.prev_online_openids:
                    will_update_progress_qqids.append(openid_qqid_map[openid])

        self.live_stat.prev_online_openids = openids
        self.live_stat.prev_online_openid_ts = curr_time

        if timespan is None:
            return
        
        for group_id in self.known_groups:
            for qq_id in will_update_progress_qqids:
                member = await self.bot.get_group_member(group_id, qq_id)
                if member is None:
                    continue
                async with self.override(member):
                    async def by(extra: 'AchvExtra'):
                        if extra.user_data is None:
                            extra.user_data = 0 # 这里单位是秒
                        
                        extra.user_data += timespan
                        extra.obtained_cnt = math.floor(extra.user_data / 3600)
                        logger.info(f'{qq_id=} LiveAchv.HUNDRED_HOURS, {extra.user_data=}')
                    await self.achv.submit(LiveAchv.HUNDRED_HOURS, by=by)


    @top_instr('绑定(账号)?', InstrAttr.FORCE_BACKUP, InstrAttr.NO_ALERT_CALLER)
    @throttle_config(name='账号绑定', max_cooldown_duration=30*60)
    async def bind_account(self, info: UserBindInfo, member: GroupMember):
        async with self.throttle as passed:
            if not passed: return
            
            if info.is_bound(): return [At(target=member.id), ' 已完成绑定, 无需重复操作']
            # if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'

            confirm_code = info.start_bind(from_group_id=member.group.id)
            # await self.nap_cat.send_msg(text='请在直播间发送弹幕(不要忘记后面的六位英文字母也要包括在弹幕中):')
            # await asyncio.sleep(1)
            # await self.nap_cat.send_msg(text=f'确认绑定{confirm_code}')
            
            # return f'已开始绑定流程, 请留意bot发送的私信'
            padding_str = "\u200C" * 15
            return [f'确认{confirm_code}绑定{padding_str}\n', At(target=member.id), ' 请直接完整复制本条消息并作为弹幕转发到直播间中']
    
    @delegate()
    async def take_lost(self, user: User, info: UserBindInfo):
        texts = [
            f'已与账号"{info.get_uname()}"完成绑定'
        ]
        openid = info.get_openid()
        if openid in self.unbound_account_caches:
            cache = self.unbound_account_caches[openid]
            self.unbound_account_caches.pop(openid)
            if cache.total_price > 0:
                texts.append(f', 并拾取了遗落的{self.price_to_voucher_count(cache.total_price)}{VOUCHER_UNIT}{VOUCHER_NAME}')
            found_item = (user.id, info)
            await self.apply_price(found_item, cache.total_price)
            # await self.apply_merit(found_item, cache.total_merit)
        return texts
        ...

    @any_instr()
    async def confirm_bind(self, cmd: str, info: Optional[UserBindInfo]):
        if info is None: return
        if not isinstance(info.bind_state, BindStateWaitConfirm): return

        if cmd == info.bind_state.two_f_code:
            await info.confirm()
            self.backup_man.set_dirty()
            return await self.take_lost()
        elif cmd.lower() in f'y{string.digits}':
            return f'请回复{info.bind_state.two_f_code}, 而不是{cmd}'
        elif 'n' in cmd.lower():
            info.unbind()
            self.backup_man.set_dirty()
            return '已取消绑定'
        
    @top_instr('解除他人绑定', InstrAttr.FORCE_BACKUP)
    async def unbind_other(self, at: At):
        async with self.admin.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                return await self.unbind_account()
            
    @top_instr('补偿', InstrAttr.FORCE_BACKUP)
    async def compensate_cmd(self, at: At, cnt: str):
        async with self.admin.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                ...

    @delegate(InstrAttr.FORCE_BACKUP)
    async def update_uname(self, info: Optional[UserBindInfo], *, name: str):
        if info is None or not info.is_bound():
            raise RuntimeError('尚未绑定账号, 无法更新')
        
        old_name = info.get_bound().uname
        info.get_bound().uname = name
        return f'已更新用户名: {old_name} -> {name}'

    @top_instr('更新用户名', InstrAttr.FORCE_BACKUP)
    async def update_uname_cmd(self, at: At, name: str):
        async with self.admin.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                return await self.update_uname(name=name)
    
    @top_instr('更新uid')
    async def update_uid_cmd(self):
        async with self.admin.privilege(type=AdminType.SUPER):
            bss = [(qq_id, info.bind_state) for qq_id, info in self.user_binds.users.items() if isinstance(info.bind_state, BindStateBound)]
            count = 0
            succ_count = 0
            fail_unames = []
            for qq_id, bs in bss:
                if bs.uid is None:
                    count += 1
                    try:
                        uid = await get_uid_by_name(bs.uname)
                        bs.uid = uid
                        self.backup_man.set_dirty()
                        logger.info(f'更新 {bs.uname} -> {uid=}')
                        succ_count += 1
                    except:
                        fail_unames.append(f'{bs.uname}({qq_id})')
                        logger.info(f'更新 {bs.uname} 失败')
        
            return '\n'.join([
                f'尝试更新{count}项, 失败{count - succ_count}项:',
                *fail_unames
            ])
            
    @delegate(InstrAttr.FORCE_BACKUP)
    async def unbind_account(self, info: Optional[UserBindInfo]):
        if info is None or not info.is_bound():
            raise RuntimeError('尚未绑定账号, 无需解除')
        uname = info.get_uname()
        info.unbind()
        return f'已解除与账号"{uname}"的绑定'

    @top_instr('解除绑定', InstrAttr.FORCE_BACKUP)
    async def unbind_account_cmd(self):
        return await self.unbind_account()

    @top_instr('宝箱提醒')
    async def enable_tmp_chest_notify_cmd(self, info: UserBindInfo, user: User):
        if not self.is_living: return '当前未开播'
        if not info.is_bound(): return BIND_HINT

        if user.id in self.live_stat.chest_notify_user_ids:
            return '您已开启宝箱提醒,无需重复开启'

        await self.voucher.adjust(
            cnt=Decimal('-0.1'), 
            extra=VoucherRecordChestNotifyTmpEnable()
        )
        self.live_stat.chest_notify_user_ids.add(user.id)
        return '已开启宝箱提醒(仅本次毛啵有效)'
        ...

    @top_instr('录屏')
    async def screen_record_cmd(self, member: GroupMember):
        if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'

        if time.time() - self.ts_last_screenshot < 3 * 60:
            return f'截屏过于频繁, 请{3 - (time.time() - self.ts_last_screenshot) // 60:.0f}分钟后再试'

        try:
            await self.x(ScreenRecord())
            self.ts_last_screenshot = time.time()
            return ['已开始录屏']
        except RuntimeError as e:
            return ''.join(['录屏失败: ', *e.args])
    
    @top_instr('切换伴奏')
    async def switch_instr_only_cmd(self, member: GroupMember, info: UserBindInfo):
        if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'
        if not info.is_bound(): return BIND_HINT

        try:
            resp = await self.x(UserSwitchInstrOnly(
                openid=info.get_openid(),
            ))
            return resp.reason
        except RuntimeError as e:
            return ''.join(['伴奏切换失败: ', *e.args])
    
    @delegate()
    async def redeem_cdkey(self, info: UserBindInfo, *, cdkey: str):
        if not info.is_bound(): return BIND_HINT

        try:
            resp = await self.x(RedeemCdkey(
                openid=info.get_openid(),
                cdkey=cdkey.upper()
            ))
            record: 'VoucherRecord' = await self.voucher.adjust(
                cnt=Decimal(resp.count) / 100,
                extra=VoucherRecordExtraLiveCdkey()
            )
            return f'兑换成功, 获得{record.count}{VOUCHER_UNIT}{VOUCHER_NAME}'
        except RuntimeError as e:
            return ''.join(['CDKEY兑换失败: ', *e.args])

    @any_instr()
    async def barcode_cdkey_cmd(self, event: GroupMessage):
        for c in event.message_chain:
            if isinstance(c, Image):
                img: PImage = await self.admin.load_image(c)
                img = img.convert("RGB")
                img = ImageOps.invert(img)
                qrcodes: list[pyzbar.pyzbar.Decoded] = pyzbar.pyzbar.decode(img, symbols=[pyzbar.pyzbar.ZBarSymbol.CODE39])
                if len(qrcodes) > 0:
                    cdkey = qrcodes[0].data.decode()
                    print(f'{cdkey=}')
                    return await self.redeem_cdkey(cdkey=cdkey)

    @top_instr('兑换码')
    async def redeem_cdkey_cmd(self, cdkey: Optional[str], info: UserBindInfo):
        if cdkey is None:
            return '缺少参数: CDKEY'
        
        return await self.redeem_cdkey(cdkey=cdkey)

    # @top_instr('截屏')
    # async def screenshot_cmd(self, member: GroupMember):
    #     if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'
    #     # if time.time() - self.ts_last_screenshot < 10 * 60:
    #     #     return f'截屏过于频繁, 请{10 - (time.time() - self.ts_last_screenshot) // 60:.0f}分钟后再试'
    #     try:
    #         return [
    #             Image(url=await self.screenshoot())
    #         ]
    #     except RuntimeError as e:
    #         return ''.join(['截屏失败: ', *e.args])
    
    @delegate()
    async def set_effect_cmd(self, member: GroupMember, *, effect_name: str):
        obtained_achvs = await self.achv.get_obtained()
        
        obtained_rare_achvs = [achv for achv in obtained_achvs if typing.cast(AchvInfo, achv.value).opts.rarity.value.level >= AchvRarity.RARE.value.level]
        if len(obtained_rare_achvs) < 3 and member.id not in config.SUPER_ADMINS:
            return '使用本功能需要达成至少三项稀有及以上级别的成就'
        if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'
        if time.time() - self.ts_last_effect_set < 5 * 60:
            return f'特效设置过于频繁, 请{5 - (time.time() - self.ts_last_effect_set) // 60:.0f}分钟后再试'
        try:
            await self.set_effect(effect_name)
            self.ts_last_effect_set = time.time()
            return '特效设置成功'
        except RuntimeError as e:
            return ''.join(['特效设置失败: ', *e.args])
        ...

    # @top_instr('镜头特效')
    # async def bobi_effect_cmd(self):
    #     return await self.set_effect_cmd(effect_name='Bobi')
    
    # @top_instr('玻璃球特效')
    # async def ball_effect_cmd(self):
    #     return await self.set_effect_cmd(effect_name='Ball')

    @top_instr('脸红特效')
    async def blush_effect_cmd(self):
        return await self.set_effect_cmd(effect_name='Blush')
    
    @top_instr('点歌')
    async def add_music_cmd(self, member: GroupMember, info: UserBindInfo, *kw: str):
        if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'
        if not info.is_bound(): return BIND_HINT

        query = ' '.join(kw)

        try:
            resp = await self.x(AddMusic(
                query=query,
                openid=info.get_openid(),
                uname=info.get_uname(),
                avatar=member.get_avatar_url()
            ))
            return resp.reason
        except RuntimeError as e:
            return ''.join(['点歌失败: ', *e.args])

        
    @top_instr('点歌队列|歌单')
    async def playlist_cmd(self, member: GroupMember):
        if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'

        print('before x')
        resp = await self.x(Playlist())
        lines = [f'{item.uname}: 《{item.music_name}》' for item in resp.queue]
        if len(lines) == 0:
            return '点歌队列空空的。'
        return '\n'.join(lines)
    
    def price_to_voucher_count(self, price: int, *, scale: int = 10):
        price_yuan = Decimal(int(price)) / 1000
        return price_yuan / scale

    async def return_voucher_for_gift_feeding(self, price: int, *, scale: int = 10):
        print('!!! return_voucher_for_gift_feeding')
        if price <= 0: return
        print('!!! gift will adjust')
        cnt = self.price_to_voucher_count(price, scale=scale)
        await self.voucher.adjust(
            cnt=cnt, 
            extra=VoucherRecordExtraLiveGift()
        )
        print('!!!done!!!')
        return cnt

    @top_instr('.*?(多久|何时).*?')
    @throttle_config(name='查询排队时长', max_cooldown_duration=3*60)
    async def fetch_music_wait_time_cmd(self, member: GroupMember, info: UserBindInfo):
        # if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'
        if not info.is_bound(): return BIND_HINT

        async with self.throttle as passed:
            if not passed: return

            resp = await self.x(FetchMusicWaitTime(
                openid=info.get_openid()
            ))
            if not resp.in_queue:
                return '还没有在排队的歌曲'
            if resp.min_duration == resp.max_duration:
                return f'还有{get_delta_time_str(resp.min_duration)}'
            else:
                return f'\n最快{get_delta_time_str(resp.min_duration)}(如果前面排队的歌曲被跳过, 可能会更早开始播放)\n最慢{get_delta_time_str(resp.max_duration)}'
    
    # def price_to_voucher_count(self, price: int):
    #     price_yuan = Decimal(int(price)) / 1000
    #     return price_yuan / 10

    # async def return_voucher_for_gift_feeding(self, price: int):
    #     if price <= 0: return
    #     await self.voucher.adjust(
    #         cnt=self.price_to_voucher_count(price), 
    #         extra=VoucherRecordExtraLiveGift()
    #     )

    @delegate()
    async def update_group_name_based_on_live_status(self, group: Group):
        conf: GroupConfigModel = await self.bot.group_config(group.id).get()
        name_comps = breakdown_chain_sync(conf.name, rf"【(.*?)】", lambda s, ctx: None)
        if self.live_stat is not None:
            if not self.live_stat.is_heating():
                name_comps = ['【配信中】', *name_comps]
            else:
                name_comps = ['【空调制热中】', *name_comps]
        next_name = ''.join(name_comps)
        if conf.name != next_name:
            await self.bot.group_config(group.id).set(conf.modify(name=next_name))

    @timer(exactly=False)
    async def update_group_name_timer(self):
        for group_id in self.known_groups:
            group = await self.bot.get_group(group_id)
            async with self.override(group):
                await self.update_group_name_based_on_live_status()

    @timer(60, exactly=False)
    async def guard_timer(self):
        room = live.LiveRoom(5288154)
        resp = await room.get_dahanghai()
        uids = [it['uid'] for it in [*resp['list'], *resp['top3']]]
        bound_users = [(qq, bs) for qq, it in self.user_binds.users.items() if (bs := it.get_bound()) is not None]

        ats = []

        for qq_id, bs in bound_users:
            uname_is_guard = bs.uid is not None and bs.uid in uids

            if uname_is_guard and bs.guard is None:
                bs.guard = Guard()
            if not uname_is_guard:
                bs.guard = None

            if bs.guard is not None and bs.guard.should_grant_reward():
                user = User(qq_id)
                async with self.override(user):
                    await self.voucher.adjust(
                        cnt=Decimal('20'),
                        extra=VoucherRecordExtraLiveGuard()
                    )
                ats.append(At(target=qq_id))
                bs.guard.mark_granted()

        if len(ats) > 0:
            self.backup_man.set_dirty()

            for group_id in self.known_groups:
                await self.bot.send_group_message(group_id, [
                    *ats,
                    f' 舰长大人, 本月的20根猫条奉上'
                ])

    @top_instr('测试地鼠')
    async def test_whac_a_mole_cmd(self, member: GroupMember, info: UserBindInfo, man: WhacAMoleMan, source: Source):
        async with self.admin.privilege(type=AdminType.SUPER):
            if not info.is_bound(): return BIND_HINT
            resp = await self.x(WhacAMole(
                openid=info.get_openid(),
                avatar=member.get_avatar_url()
            ))

    @top_instr('地鼠|踩我')
    @throttle_config(name='打地鼠', max_cooldown_duration=5*60)
    async def whac_a_mole_cmd(self, member: GroupMember, info: UserBindInfo, man: WhacAMoleMan, source: Source):
        if not self.is_living and member.id not in config.SUPER_ADMINS: return '当前未开播'
        if not info.is_bound(): return BIND_HINT

        if not isinstance(man.question_state, (QuestionStateIdle, QuestionStateAnswerExplanationConfirmation)):
            return '请先回答上一个问题'

        if member.id not in config.SUPER_ADMINS:
            passed = await self.throttle.do_associated()
            if not passed: return

        question_bank_path = self.path.data['question_bank']
        question_file_name = random.choice([fn for fn in os.listdir(question_bank_path) if not fn.startswith('.')])
        print(f'{question_file_name=}')
        
        man.update_state_by_question_file_name(question_file_name, source)

        resp = await source.op.send([
            At(target=member.id),
            ' ', 
            *man.question_state.get_prompt(),
            mirai.models.message.Image(path=os.path.join(question_bank_path, question_file_name))
        ])

        if isinstance(man.question_state, QuestionStateSingleChoice):
            man.question_state.recall_item = await self.recall_scheduler.add_item(it=RecallItem(
                msg_id=resp.message_id,
                target_id=source.get_target_id(),
            ))

    @any_instr()
    async def music_select(self, text: str, man: Optional[MusicMan], info: UserBindInfo):
        if man is None: return
        if not isinstance(man.state, MusicStateSelect): return

        song_order = re.search('\d+|一|二|三|四|五', text)
        if song_order is None:
            if time.time() - man.state.created_ts > 5 * 60:
                man.state = MusicStateIdle()
                self.backup_man.set_dirty()
            return
        
        order = int(cn2an.cn2an(song_order.group(), 'smart'))

        try:
            await self.mqtt_client.publish(f'/live/event/select_music', json.dumps({
                'uid': info.get_openid(),
                'uname': info.get_uname(),
                'order': order,
                'id': man.state.id
            }))
        finally:
            man.state = MusicStateIdle()
            self.backup_man.set_dirty()

    @delegate()
    async def try_show_image(self, info: UserBindInfo, *, img: Image):
        # if not self.is_living: return
        if not info.is_bound(): return

        await self.mqtt_client.publish(f'/live/event/image', json.dumps({
            'openid': info.get_openid(),
            'url': img.url,
        }))

    @any_instr()
    async def check_whac_a_mole_answer(self, text: str, man: Optional[WhacAMoleMan], member: GroupMember, info: UserBindInfo, source: Source):
        if man is None: return

        if isinstance(man.question_state, QuestionStateSingleChoice):
            question_name = man.question_state.get_question_name()
            res = man.question_state.get_result(text)
            if res is None: return

            to_idle = True
            try:
                man.question_state.recall_item.recall_after(5 * 60) # 不必要, 防止忘记撤回

                if res is AnswerResult.CORRECT:
                    await self.achv.submit(LiveAchv.CIVIL_SERVANT)
                    man.question_state.recall_item.recall()
                    await source.op.send([At(target=source.get_member_id()), ' 回答正确'])
                    try:
                        resp = await self.x(WhacAMole(
                            openid=info.get_openid(),
                            avatar=member.get_avatar_url()
                        ))

                        await self.throttle.reset(fn=self.whac_a_mole_cmd)
                        return f'在第{resp.position + 1}个坑位生成了一只地鼠'
                    except RuntimeError as e:
                        return ''.join(['地鼠生成失败: ', *e.args])
                    # return ['回答正确']
                if res is AnswerResult.WRONG:
                    await self.throttle.reset(fn=self.whac_a_mole_cmd)
                    explanations = self.load_answer_explanations()
                    if question_name in explanations and await self.voucher.is_satisfied(cnt=1):
                        to_idle = False
                        man.question_state = QuestionStateAnswerExplanationConfirmation(
                            explanation=explanations[question_name],
                            recall_item=man.question_state.recall_item
                        )
                        self.backup_man.set_dirty()
                        return [f'回答错误, 是否消耗一{VOUCHER_UNIT}{VOUCHER_NAME}获取答案解析? 回复"y"确认, 回复其他取消']
                    else:
                        man.question_state.recall_item.recall()
                        return ['回答错误']
            finally:
                if to_idle:
                    man.to_idle_state()
                    self.backup_man.set_dirty()
        if isinstance(man.question_state, QuestionStateAnswerExplanationConfirmation):
            res = man.question_state.should_show(text)
            if res is None: return
            try:
                explanation = man.question_state.explanation
                if res:
                    try:
                        await self.voucher.adjust(
                            cnt=Decimal('-1'), 
                            extra=VoucherRecordExtraLiveAnswerExplanation()
                        )
                        man.question_state.recall_item.recall_after(5 * 60)
                        return [explanation]
                    except:
                        man.question_state.recall_item.recall()
                else:
                    man.question_state.recall_item.recall()
                    ...
            finally:
                man.to_idle_state()
                self.backup_man.set_dirty()
            
    @timer
    async def question_expired_check_timer(self):
        async def next(group_id: int, member_id: int, man: WhacAMoleMan):
            if isinstance(man.question_state, QuestionStateSingleChoice):
                if not man.question_state.is_expired(): return
                try:
                    await man.question_state.source.op.send([At(target=man.question_state.source.get_member_id()), ' 问题已超时'])
                    man.question_state.recall_item.recall()
                    member = await self.bot.get_group_member(group_id, member_id)
                    async with self.override(member):
                        await self.throttle.reset(fn=self.whac_a_mole_cmd)
                finally:
                    man.to_idle_state()
                    self.backup_man.set_dirty()

            if isinstance(man.question_state, QuestionStateAnswerExplanationConfirmation):
                if not man.question_state.is_expired(): return
                man.question_state.recall_item.recall()
                man.to_idle_state()
                self.backup_man.set_dirty()

        await asyncio.gather(*[next(group_id, member_id, man) for group_id, item_group in self.gls_whac_a_mole.groups.items() for member_id, man in item_group.items()])

    async def on_live_stopped(self):
        logger.debug('结束了')
        if self.live_stat is not None:
            digest = self.live_stat.build_digest()
            try:
                await self.update_audience_online_time(self.live_stat.prev_online_openids)
            except:
                traceback.print_exc()
            
            self.live_stat = None
            
            # 下播数据除了总时长，还打算加入空调制热时长，心率曲线（曲线里又被制热的区间背景变成浅红色），最大心率，地鼠逃脱/生成数，歌曲点歌/总播放数

            for group_id in self.known_groups:
                group = await self.bot.get_group(group_id)
                async with self.override(group):
                    # TODO
                    await self.update_group_name_based_on_live_status()
                    await self.bot.send_group_message(group_id, [f'下播！本次啵了{digest.formatted_duration}\n不要忘了使用指令【#兑换码 毛啵间的6位代码】或者把“清晰的条形码图片”发到群里可以领取{VOUCHER_NAME}哦。限时五分钟'])

    @timer
    async def watch_dog(self):
        if self.is_living and self.live_stat.is_timeout():
            await self.on_live_stopped()

    @timer
    async def remove_expired_pendings(self):
        self.whac_a_mole_pendings = {k: v for k, v in self.whac_a_mole_pendings.items() if time.time() - v.created_ts < 60}
        self.pendings = {k: v for k, v in self.pendings.items() if time.time() < v.expired_ts}

    def load_answer_explanations(self) -> dict:
        with open(self.path.data.of_file('answer_explanation.json'), encoding='utf-8') as f:
            return json.load(f)

    @autorun
    async def conn_to_live_mqtt(self):
        self.mqtt_client = Client(
            "uf90fbf8.ala.cn-hangzhou.emqxsl.cn", 
            port=8883, 
            tls_params=aiomqtt.TLSParameters(
                ca_certs=self.path.data.of_file('emqxsl-ca.crt'),
            ), 
            username='guest', 
            password='guest'
        )
        interval = 5  # Seconds
        while True:
            try:
                async with self.mqtt_client:
                    logger.info('Connected')
                    await self.mqtt_client.subscribe("/live/status/+")
                    await self.mqtt_client.subscribe("/live/resp/+")
                    await self.mqtt_client.subscribe("/live/event/+")
                    await self.mqtt_client.publish('/live/query/status')
                    async for message in self.mqtt_client.messages:
                        try:
                            await self.handle_message(message)
                        except: 
                            traceback.print_exc()
            except aiomqtt.MqttError:
                logger.warning(f"Connection lost; Reconnecting in {interval} seconds ...")
                await asyncio.sleep(interval)
            except:
                traceback.print_exc()
