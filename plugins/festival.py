from datetime import datetime, date, timedelta
from mirai import At, MessageEvent, Plain
from plugin import AchvCustomizer, Inject, InstrAttr, Plugin, any_instr, delegate, route, top_instr
from utilities import AchvEnum, AchvExtra, AchvOpts, AchvRarity, AdminType, GroupMemberOp
from borax.calendars.festivals2 import FestivalLibrary, Festival as Fes, SolarFestival
from dataclasses import dataclass
import re

from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from plugins.achv import Achv
    from plugins.nap_cat import NapCat
    from plugins.admin import Admin

class FestivalAchv(AchvEnum):
    MID_AUTUMN_FESTIVAL = 0, '月饼', '在中秋节当天发送"中秋快乐"', AchvOpts(rarity=AchvRarity.RARE, display='🥮', dynamic_deletable=True)
    NATIONAL_DAY = 1, '国旗', '在国庆节当天发送"国庆快乐"', AchvOpts(rarity=AchvRarity.RARE, display='🇨🇳', dynamic_deletable=True)
    FURSUIT_FRIDAY = 2, '肉垫', '在毛毛星期五当天发送包含"毛五"的消息', AchvOpts(rarity=AchvRarity.UNCOMMON, display='🐾', dynamic_deletable=True)
    SPRING_FESTIVAL = 3, '爆竹', '在春节当天发送"新年快乐"', AchvOpts(rarity=AchvRarity.RARE, display='🧨', dynamic_deletable=True)
    CHRISTMAS = 4, '圣诞树', '在圣诞节当天发送"圣诞快乐"', AchvOpts(rarity=AchvRarity.RARE, display='🎄', dynamic_deletable=True)
    PROGRAMMERS_DAY = 5, '程序员', '在程序员节当天发送"程序员"、"1024"等关键字', AchvOpts(rarity=AchvRarity.RARE, display='👨‍💻', dynamic_deletable=True)
    PUMPKIN = 6, '南瓜', '在圣诞节当天发送"万圣节"等关键字', AchvOpts(rarity=AchvRarity.RARE, display='🎃', dynamic_deletable=True)
    LOVE = 7, '爱心', '在5月20日当天发送"爱"、"喜欢"、"520"等关键字', AchvOpts(rarity=AchvRarity.RARE, display='❤️', dynamic_deletable=True)
    BIRTHDAY = 8, '蛋糕', '群友生日那天自动获得', AchvOpts(rarity=AchvRarity.LEGEND, display='🎂', custom_obtain_msg='生日快乐!', dynamic_name=True)

class FursuitFriday():
    def countdown(self, date_obj: date = None):
        if date_obj is None:
            date_obj = date.today()
        days_ahead = 4 - date_obj.weekday()  # 4代表周五
        if days_ahead < 0:  # 如果今天是周五，返回7天后
            days_ahead += 7
        return days_ahead, None
    
    @property
    def name(self):
        return '毛毛星期五'
    
@dataclass
class FestivalItem():
    festival: Fes
    trigger_regex: str
    associated_achv: FestivalAchv
    duration_days: int = 1
    offset_days: int = 0

    def is_available(self):
        # 比如今天是30，目标是1号，那么需要把今天+1 offset是-1
        offset_date = date.today() - timedelta(days=self.duration_days-1+self.offset_days)
        days, _ = self.festival.countdown(offset_date)
        return days < self.duration_days

@route('节日')
class Festival(Plugin, AchvCustomizer):
    achv: Inject['Achv']
    nap_cat: Inject['NapCat']
    admin: Inject['Admin']
    
    def __init__(self):
        self.library = FestivalLibrary.load_builtin()
        self.festivals = [
            FestivalItem(
                festival=self.library.get_festival('中秋节'),
                trigger_regex='中秋.*?快乐',
                associated_achv=FestivalAchv.MID_AUTUMN_FESTIVAL
            ),
            FestivalItem(
                festival=self.library.get_festival('国庆节'),
                trigger_regex='国庆.*?快乐',
                associated_achv=FestivalAchv.NATIONAL_DAY,
                duration_days=7
            ),
            FestivalItem(
                festival=FursuitFriday(), 
                trigger_regex='毛五',
                associated_achv=FestivalAchv.FURSUIT_FRIDAY
            ),
            FestivalItem(
                festival=self.library.get_festival('春节'),
                trigger_regex='(新年|除夕).*?快乐',
                associated_achv=FestivalAchv.SPRING_FESTIVAL,
                duration_days=15,
                offset_days=-1
            ),
            FestivalItem(
                festival=self.library.get_festival('圣诞节'),
                trigger_regex='圣诞.*?快乐|christmas',
                associated_achv=FestivalAchv.CHRISTMAS
            ),
            FestivalItem(
                festival=SolarFestival(month=10,day=24),
                trigger_regex=r'程序员|10.?24',
                associated_achv=FestivalAchv.PROGRAMMERS_DAY
            ),
            FestivalItem(
                festival=SolarFestival(month=10,day=31),
                trigger_regex=r'万圣节',
                associated_achv=FestivalAchv.PUMPKIN
            ),
            FestivalItem(
                festival=SolarFestival(month=5,day=20),
                trigger_regex=r'爱|喜欢|520',
                associated_achv=FestivalAchv.LOVE,
                duration_days=2,
            ),
        ]
        ...

    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def festival_achv(self, event: MessageEvent, op: GroupMemberOp):
        for item in self.festivals:
            if item.is_available():
                if not await self.achv.has(item.associated_achv):
                    for c in event.message_chain:
                        if isinstance(c, Plain) and re.search(item.trigger_regex, c.text) is not None:
                            break
                    else: return
                    
                    await self.achv.submit(item.associated_achv, silent=True)
                    # await op.nudge()
                    await self.nap_cat.send_poke()

    @delegate()
    async def is_achv_deletable(self, e: AchvEnum):
        for item in self.festivals:
            if item.associated_achv is e:
                return not item.is_available()
        return False
    
    async def get_achv_name(self, e: 'AchvEnum', extra: Optional['AchvExtra']) -> str:
        if e is FestivalAchv.BIRTHDAY and extra.obtained_ts is not None:
            dt = datetime.fromtimestamp(extra.obtained_ts)
            return f"{dt.month}月{dt.day}日的蛋糕"
        return e.aka

    def get_countdowns(self):
        return {item.festival.name: item.festival.countdown()[0] for item in self.festivals}

    @top_instr('生日')
    async def give_birthday_cmd(self, at: At):
        async with self.admin.privilege(type=AdminType.SUPER):
            member = await self.member_from(at=at)
            async with self.override(member):
                await self.achv.submit(FestivalAchv.BIRTHDAY)

    @top_instr('节日测试')
    async def test_fes(self, name: str):
        library = FestivalLibrary.load_builtin()
        fes = library.get_festival(name)

        days, _ = fes.countdown()

        return [f'{days=}']
        ...