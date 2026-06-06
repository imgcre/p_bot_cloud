from dataclasses import dataclass, field
from enum import Enum, auto
import random
import time
from typing import Final
import typing
from event_types import AchvObtainedEvent, EffectiveSpeechEvent
from plugin import Plugin, delegate, enable_backup, route, Inject
from utilities import AchvEnum, AchvInfo, AchvOpts, AchvRarity, GroupSpec, handler
from mirai.models.entities import GroupMember

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.achv import Achv

class BatAchv(AchvEnum):
    MIGIC_CIRCLE = 0, '魔法阵', '通过一般方式获得小蝙蝠，而后小蝙蝠转移后遗留的魔法阵', AchvOpts(rarity=AchvRarity.EPIC, display='🔮')
    PORTAL = 1, '传送门', '通过转移方式获得小蝙蝠，而后小蝙蝠转移后遗留的传送门', AchvOpts(rarity=AchvRarity.UNCOMMON, display='🕳️')

class TransferReason(Enum):
    ACTIVE = auto() # 主动转移
    ACHV = auto() # 群友获取到小蝙蝠成就导致的转移

@dataclass
class TransferRecord():
    target_member_id: int
    reason: TransferReason
    created_ts: float = field(default_factory=time.time)

@dataclass
class BatMan():
    transfer_records: list[TransferRecord] = field(default_factory=list) # 包含转移的日期和转移到的目标群友，还有转移原因
    owner_last_speak_ts: int = 0 # 持有小蝙蝠的人的最后发言时间

    def append_record(self, record: TransferRecord):
        self.transfer_records.append(record)

    def update_last_speak_ts(self):
        self.owner_last_speak_ts = time.time()

@route('小蝙蝠')
@enable_backup
class Bat(Plugin):
    gs_bat: GroupSpec[BatMan] = GroupSpec[BatMan]()

    achv: Inject['Achv']

    BAT_TRANSFERRED_MAGIC_CNT: Final = 58259

    # 群里最多只有一只小蝙蝠
    # 如果群友是通过积累的方式获得小蝙蝠的，那么小蝙蝠转移的时候他会获得【原初传送门】（史诗）
    # 当有另外的群友通过累积方式获得小蝙蝠时，小蝙蝠会强制转移
    # 小蝙蝠在一般情况下会按一定的条件转移到最近发言的五个人中的一人身上
    # 每个群都有独立的小蝙蝠轨迹, 记录着小蝙蝠的转移信息

    # @handler
    # @delegate()
    # async def on_effective_speech(self, event: EffectiveSpeechEvent, man: BatMan, member: GroupMember):
    #     from plugins.fur import FurAchv
        
    #     obtained_member_ids = await self.achv.get_obtained_member_ids(FurAchv.BAT)
    #     if len(obtained_member_ids) == 0:
    #         return
        
    #     if member.id in obtained_member_ids:
    #         man.update_last_speak_ts()
    #         return
        
    #     time_span = time.time() - man.owner_last_speak_ts
    #     prob = time_span // (60 * 60) * 0.1

    #     if random.random() < prob:
    #         collected_count = await self.achv.get_achv_process(FurAchv.BAT)
    #         if collected_count is None:
    #             collected_count = 0
    #         await self.achv.submit(FurAchv.BAT, override_obtain_cnt=collected_count+self.BAT_TRANSFERRED_MAGIC_CNT)
    #         man.update_last_speak_ts()

    # @handler
    # @delegate()
    # async def on_achv_obtained(self, event: AchvObtainedEvent, man: BatMan, member: GroupMember):
    #     from plugins.fur import FurAchv

    #     if event.e is FurAchv.BAT:
    #         man.append_record(TransferRecord(
    #             target_member_id=member.id,
    #             reason=TransferReason.ACHV 
    #                 if await self.achv.get_achv_process(event.e) < self.BAT_TRANSFERRED_MAGIC_CNT 
    #                 else TransferReason.ACTIVE
    #         ))

    #         self.backup_man.set_dirty()
            
    #         # 有新群友获得了成就, 清除所有旧群友的BAT成就并替换成传送门
    #         obtained_member_ids: set[int] = await self.achv.get_obtained_member_ids(event.e)
    #         if member.id in obtained_member_ids:
    #             obtained_member_ids.remove(member.id)

    #         for member_id in obtained_member_ids:
    #             member = await self.member_from(member_id=member_id)
    #             async with self.override(member):
    #                 collected_count = await self.achv.get_achv_process(event.e)
    #                 if collected_count is not None:
    #                     if collected_count >= self.BAT_TRANSFERRED_MAGIC_CNT:
    #                         # 通过转移获得的小蝙蝠
    #                         await self.achv.submit(BatAchv.PORTAL)
    #                     else:
    #                         # 通过累积获得的小蝙蝠
    #                         await self.achv.submit(BatAchv.MIGIC_CIRCLE)

    #                     collected_count %= self.BAT_TRANSFERRED_MAGIC_CNT

    #                     info = typing.cast(AchvInfo, event.e.value)
    #                     collected_count %= info.opts.target_obtained_cnt
    #                     if collected_count == 0:
    #                         await self.achv.remove(event.e, force=True)
    #                     else:
    #                         await self.achv.submit(event.e, override_obtain_cnt=collected_count)
    #                 else:
    #                     # 通过转移获得的小蝙蝠
    #                     await self.achv.submit(BatAchv.PORTAL)
    #                     await self.achv.remove(event.e, force=True)
