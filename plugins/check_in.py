from dataclasses import dataclass, field
from decimal import Decimal
import random
from typing import List, Optional
import typing
from mirai import At, GroupMessage
from plugin import AchvCustomizer, Inject, Plugin, any_instr, delegate, enable_backup, nudge_instr, top_instr, route, InstrAttr
from mirai.models.message import Image
from mirai.models.entities import GroupMember
from utilities import AchvEnum, AchvExtra, AchvOpts, AchvRarity, AdminType, GroupLocalStorage, GroupLocalStorageAsEvent, GroupMemberOp, ProxyContext, Source, VoucherRecordExtraReCheckIn, get_logger, throttle_config
import pytz
from datetime import datetime
import time
import calendar
from mirai.models.events import NudgeEvent
from mirai.models.message import MarketFace

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.renderer import Renderer
    from plugins.achv import Achv
    from plugins.admin import Admin
    from plugins.throttle import Throttle
    from plugins.nap_cat import NapCat
    from plugins.voucher import Voucher

logger = get_logger()

class CheckInAchv(AchvEnum):
    CHAMPION = 0, '火急火燎', '获得某日签到第一名', AchvOpts(display='🚀')
    CONSECUTIVE_DAYS_5 = 1, '连五鞭', '连续签到五天', AchvOpts(rarity=AchvRarity.UNCOMMON, custom_obtain_msg='打出了闪电五连鞭', display_pinned=True, display='⚡', display_weight=-1, target_obtained_cnt=5, custom_remove=True, unit='连击', dynamic_name=True)
    PERFECT_ATTENDANCE = 2, '全勤', '连续签满一个自然月', AchvOpts(rarity=AchvRarity.RARE, display='🈵')
    UNITY_IS_STRENGTH = 3, '众人拾柴火焰高', '同一天有50人及以上参与签到', AchvOpts(rarity=AchvRarity.EPIC, display='🤝')
    HUGGING_FACE = 4, '助人为乐', '帮助他人签到100次', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='抱了抱大家', target_obtained_cnt=100, display='🤗')
    CHECKED_IN_TODAY = 5, '已签到', '今日已签到时自动获取', AchvOpts(display_pinned=True, locked=True, hidden=True, display='✨️', display_weight=-1, dynamic_obtained=True)
    EARLY_BIRD = 6, '早起的鸟', '每天前10位签到有25%的概率积累进度', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='起得好早', display='🦉', target_obtained_cnt=100)

class AlreadyCheckInException(Exception):
    def __init__(self):
        super().__init__('今天已经签到过了')

class CheckInRequiredException(Exception):
    ...

class BadTimeException(Exception):
    ...

@dataclass
class CheckInMan():
    checkin_ts: List[float] = field(default_factory=list)

    def get_checkin_ts_today(self):
        if len(self.checkin_ts) == 0: return None
        last_checkin_ts = self.checkin_ts[-1]
        if last_checkin_ts < self.get_start_ts_of_day(): return None
        return last_checkin_ts


    def check_in(self):
        if self.get_checkin_ts_today() is not None:
            raise AlreadyCheckInException()
        now = time.time()
        self.checkin_ts.append(now)

        return now
    
    def is_check_in_that_day(self, target_ts):
        start_ts_of_that_day = self.get_start_ts_of_day(ts=target_ts)
        end_ts_of_that_day = start_ts_of_that_day + 60 * 60 * 24
        return any([e >= start_ts_of_that_day and e < end_ts_of_that_day for e in self.checkin_ts])

    def check_re_check_in_allowed(self, target_ts):
        if target_ts > time.time():
            raise BadTimeException()
        if self.is_check_in_that_day(target_ts):
            raise AlreadyCheckInException()

    def re_check_in(self, target_ts: float):
        self.check_re_check_in_allowed(target_ts)
        self.ordered_insert(self.checkin_ts, target_ts)

    @staticmethod
    def ordered_insert(li: list, e):
        try:
            target_index = list(x > e for x in li).index(True)
            li.insert(target_index, e)
        except:
            li.append(e)

    @property
    def consecutive_days(self):
        one_day_span = 60 * 60 * 24
        curr_ts = self.get_start_ts_of_day()
        cnt = 0
        while cnt < len(self.checkin_ts):
            if self.checkin_ts[-(cnt+1)] < curr_ts:
                break
            cnt += 1
            curr_ts -= one_day_span
        return cnt

    @property
    def checkin_ts_this_month(self):
        return [ts for ts in self.checkin_ts if ts >= self.get_start_ts_of_this_month()]

    @classmethod
    def get_start_ts_of_day(cls, *, ts=None):
        return cls.get_start_ts_of(hour=0, minute=0, second=0, microsecond=0, ts=ts)
    
    @classmethod
    def get_start_ts_of_this_month(cls, *, ts=None):
        return cls.get_start_ts_of(hour=0, minute=0, second=0, microsecond=0, day=1, ts=ts)
    
    @classmethod
    def if_full_checked_in_this_month(cls, consecutive_days):
        tz = pytz.timezone('Asia/Shanghai')
        today = datetime.now(tz=tz)
        last_day_this_month = calendar.monthrange(today.year, today.month)[1]
        return consecutive_days >= last_day_this_month and today.day == last_day_this_month

    @staticmethod
    def get_start_ts_of(*, ts=None, **kwargs):
        tz = pytz.timezone('Asia/Shanghai')
        if ts is None:
            ts = time.time()
        today = datetime.fromtimestamp(ts, tz=tz)
        start = today.replace(**kwargs)
        return start.timestamp()


@route('check_in')
@enable_backup
class CheckIn(Plugin, AchvCustomizer):
    gls: GroupLocalStorage[CheckInMan] = GroupLocalStorage[CheckInMan]()
    renderer: Inject['Renderer']
    achv: Inject['Achv']
    admin: Inject['Admin']
    throttle: Inject['Throttle']
    nap_cat: Inject['NapCat']
    voucher: Inject['Voucher']

    @delegate()
    async def is_checked_in_today(self, man: Optional[CheckInMan]):
        return man is not None and man.get_checkin_ts_today() is not None

    async def is_achv_obtained(self, e: 'AchvEnum'):
        if e is CheckInAchv.CHECKED_IN_TODAY:
            return await self.is_checked_in_today()
        return False
    
    async def get_achv_name(self, e: 'AchvEnum', extra: Optional['AchvExtra']) -> str:
        if e is CheckInAchv.CONSECUTIVE_DAYS_5:
            cnt = extra.obtained_cnt if extra is not None else 0
            if cnt < 0:
                cnt = 0
            if cnt > 5:
                cnt = 5
            return ['没鞭', '一鞭', '连两鞭', '连三鞭', '连四鞭', '连五鞭'][cnt]
        return e.aka
    
    @delegate()
    async def calc_consecutive_days_5_removed_count(self, man: Optional[CheckInMan]):
        if man is None: return 0
        consecutive_days = man.consecutive_days
        calced_cnt = max(0, min(consecutive_days, 5) - 1)
        logger.info(f'{consecutive_days=}, {calced_cnt=}')
        return calced_cnt
    
    async def remove_achv(self, e: 'AchvEnum', extra: 'AchvExtra'):
        if e is CheckInAchv.CONSECUTIVE_DAYS_5:
            if extra is not None:
                extra.obtained_cnt = await self.calc_consecutive_days_5_removed_count()
    
    @delegate()
    async def get_checkin_ts_today(self, man: Optional[CheckInMan]):
        if man is None:
            return None
        return man.get_checkin_ts_today()

    @delegate()
    async def query_missing(self, man: Optional[CheckInMan]):
        if man is None: return None

        one_day_span = 60 * 60 * 24

        remain_ts = man.checkin_ts_this_month

        no_days = []
        
        for curr_ts in range(int(man.get_start_ts_of_this_month()), int(man.get_start_ts_of_day()), one_day_span):
            prev_len = len(remain_ts)
            remain_ts = [ts for ts in remain_ts if not(curr_ts <= ts < curr_ts + one_day_span)]
            if prev_len == len(remain_ts):
                # 在这一天没有签到过
                no_days.append(curr_ts)
        
        return no_days
    
    @staticmethod
    def ts_to_date_str(ts: float):
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    
    @staticmethod
    def ts_from_date_str(text: str):
        tz = pytz.timezone('Asia/Shanghai')
        dt = tz.localize(datetime.strptime(text, '%Y-%m-%d'))
        return dt.timestamp() + 1

    @top_instr('漏签查询')
    async def query_missing_cmd(self):
        no_days = await self.query_missing()

        if no_days is None:
            return '还没有签到过'
        
        if len(no_days) == 0:
            return '本月没有漏签'
        
        if len(no_days) > 5:
            return '本月漏签天数超过5天'
        
        return '\n'.join(['在以下日期漏签了:', *[self.ts_to_date_str(ts) for ts in no_days]])

    @top_instr('签到|起床|醒来', InstrAttr.NO_ALERT_CALLER)
    async def check_in(self):
        await self.admin.check_proxy(disable_required=True)
        return await self.do_check_in()

    @delegate()
    async def batch_check_in(self, *, members: list[GroupMember]):
        checked: list[GroupMember] = []
        for member in members:
            try:
                async with self.override(member):
                    await self.do_check_in(raise_error=True)
                checked.append(member)
            except:
                ...
        return checked

    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def quick_check_in(self, text: str):
        if text == '签到':
            return await self.do_check_in()

    @top_instr('帮群友签到', InstrAttr.NO_ALERT_CALLER)
    @throttle_config(name='互帮互助', max_cooldown_duration=4*60*60)
    async def check_in_proxy(self, at: At):
        async with self.throttle as passed:
            if not passed: return

            member = await self.member_from(at=at)
            async with self.override(member):
                await self.do_check_in(raise_error=True)
            await self.achv.submit(CheckInAchv.HUGGING_FACE)
    
    @any_instr(InstrAttr.INTERCEPT_EXCEPTIONS)
    async def check_in_via_motion(self, event: GroupMessage):
        for c in event.message_chain:
            if (isinstance(c, MarketFace) and c.id == 236744 and c.name == '[被拖走]') or (isinstance(c, Image) and c.image_id == 'https://gxh.vip.qq.com/club/item/parcel/item/6c/6c13270ec4dd60145ed9c5f3be9a71cf/raw300.gif'):
                await self.do_check_in(raise_error=True)
                return
    
    @nudge_instr(InstrAttr.INTERCEPT_EXCEPTIONS)
    async def nudge(self, event: NudgeEvent):
        if event.target != self.bot.qq:
            return
        await self.do_check_in(raise_error=True)

    @top_instr('超级补签')
    async def super_re_check_in_cmd(self, man: CheckInMan):
        start_ts = self.ts_from_date_str('2025-08-13')
        end_ts = self.ts_from_date_str('2025-08-24')

        curr_ts = start_ts + 12 * 60 * 60
        while curr_ts < end_ts:
            try:
                if man.is_check_in_that_day(curr_ts):
                    continue
                man.re_check_in(curr_ts)
            finally:
                curr_ts += 12 * 60 * 60
        
        return '好咯'

    
    @top_instr('补签', InstrAttr.NO_ALERT_CALLER)
    async def re_check_in_cmd(self, date_text: Optional[str]):
        no_days = await self.query_missing()
        if no_days is not None and len(no_days) == 0:
            return '本月没有漏签, 不需要补签'
        if date_text is None:
            return '请提供日期参数, 如: #补签 2025-6-1'
        return await self.re_check_in(date_text=date_text)
    
    @top_instr('取消签到', InstrAttr.FORCE_BACKUP)
    async def cancel_check_in_cmd(self, man: CheckInMan):
        async with self.admin.privilege(type=AdminType.SUPER):
            man.checkin_ts = [ts for ts in man.checkin_ts if ts < man.get_start_ts_of_day()]
        
    @top_instr('帮群友补签', InstrAttr.NO_ALERT_CALLER, InstrAttr.FORCE_BACKUP)
    async def re_check_in_to_cmd(self, at: At, date_text: str):
        async with self.admin.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                return await self.re_check_in(date_text=date_text, consume_voucher=False)
            
    @top_instr('刷新连五鞭', InstrAttr.NO_ALERT_CALLER, InstrAttr.FORCE_BACKUP)
    async def refresh_c5_cmd(self, at: At):
        async with self.admin.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                return await self.refresh_c5()
            
    @delegate()
    async def refresh_c5(self, man: Optional[CheckInMan]):
        if man is None:
            return '无签到记录'
        
        consecutive_days = man.consecutive_days
        await self.achv.submit(CheckInAchv.CONSECUTIVE_DAYS_5, override_obtain_cnt=min(consecutive_days, 5), silent=True)
        return f'ok, {consecutive_days=}'
    
    @delegate()
    async def re_check_in(self, man: CheckInMan, member: GroupMember, *, date_text: str, consume_voucher: bool=True):
        try:
            ts = self.ts_from_date_str(date_text)
            man.check_re_check_in_allowed(ts)
            if consume_voucher:
                await self.voucher.adjust(
                    cnt=Decimal('-0.1'), 
                    extra=VoucherRecordExtraReCheckIn()
                )
            man.re_check_in(ts)
        except AlreadyCheckInException:
            return f'{member.member_name}在{date_text}那天已经签到过了'
        except BadTimeException:
            return f'签到日期不正确'

        consecutive_days = man.consecutive_days
        await self.achv.submit(CheckInAchv.CONSECUTIVE_DAYS_5, override_obtain_cnt=min(consecutive_days, 5), silent=True)
        # if consecutive_days >= 5:
        #     await self.achv.submit(CheckInAchv.CONSECUTIVE_DAYS_5)

        if man.if_full_checked_in_this_month(consecutive_days):
            await self.achv.submit(CheckInAchv.PERFECT_ATTENDANCE)

        # b64_img = await self.renderer.render('check-in', duration=5, keep_last=True, data={
        #     'ranking': 99,
        #     'checkin_ts_this_month': man.checkin_ts_this_month,
        #     'avatar_url': member.get_avatar_url()
        # })
        # return [
        #     Image(base64=b64_img)
        # ]

        return 'ok'
    


    @delegate(InstrAttr.FORCE_BACKUP)
    async def do_check_in(self, glse_: gls.event_t(), group_member: GroupMember, source: Source, *, raise_error = False, silent = False):

        glse = typing.cast(GroupLocalStorageAsEvent[CheckInMan], glse_)
        man = glse.get_or_create_data()
        try:
            checkin_tsc = man.check_in()
        except AlreadyCheckInException:
            if raise_error:
                raise
            action = '签到' if group_member.id != 3372099218 else '卢'
            return [At(target=group_member.id), f' 今天已经{action}过了']
        ranking = sorted([
            ts for v in glse.get_data_of_group().values() 
            if (ts := v.get_checkin_ts_today()) is not None
        ]).index(checkin_tsc) + 1

        if ranking == 1:
            await self.achv.submit(CheckInAchv.CHAMPION, silent=silent)

        if ranking <= 10 and random.random() < 0.25:
            await self.achv.submit(CheckInAchv.EARLY_BIRD)

        # if ranking >= 50:
        #     for member_id, man in [
        #         it for it in glse.get_data_of_group().items() if it[1].get_checkin_ts_today() is not None
        #     ]:
        #         member = await self.member_from(member_id=member_id)
        #         async with self.override(member):
        #             await self.achv.submit(CheckInAchv.UNITY_IS_STRENGTH, silent=True)
        
        consecutive_days = man.consecutive_days
        await self.achv.submit(CheckInAchv.CONSECUTIVE_DAYS_5, override_obtain_cnt=min(consecutive_days, 5), silent=True)

        if man.if_full_checked_in_this_month(consecutive_days):
            await self.achv.submit(CheckInAchv.PERFECT_ATTENDANCE, silent=silent)

        await self.admin.dec_violation_cnt()

        await self.achv.update_member_name()

        msg_id = source.get_message_id()
        if msg_id is not None:
            await self.nap_cat.set_msg_emoji_like(msg_id, 124)
        else:
            await self.nap_cat.send_poke()

        if not silent:
            return [f'今天第{ranking}个签到']
            ...
            # await self.renderer.render_as_task(url='check-in', duration=5, keep_last=True, data={
            #     'ranking': ranking,
            #     'checkin_ts_this_month': man.checkin_ts_this_month,
            #     'avatar_url': op.get_avatar()
            # })
            # b64_img = await self.renderer.render('check-in', duration=5, keep_last=True, data={
            #     'ranking': ranking,
            #     'checkin_ts_this_month': man.checkin_ts_this_month,
            #     'avatar_url': op.get_avatar()
            # })
            # return [
            #     Image(base64=b64_img)
            # ]
            

