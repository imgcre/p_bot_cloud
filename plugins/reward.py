
from dataclasses import dataclass, field
from decimal import Decimal
from enum import EnumMeta
import inspect
import sys
import time
import typing
from plugin import Inject, InjectNotifier, Plugin, delegate, enable_backup, route, top_instr

from typing import TYPE_CHECKING

from utilities import VOUCHER_NAME, VOUCHER_UNIT, AdminType, RewardCategoryInfo, RewardEnum, RewardInfo, UserSpec, VoucherRecordExtraReward, get_logger
if TYPE_CHECKING:
    from plugins.voucher import Voucher, VoucherRecord
    from plugins.admin import Admin

logger = get_logger()

@dataclass
class RewardRecordItem():
    consume_id: float
    reward: RewardEnum
    created_ts: int = field(default_factory=time.time)

@dataclass
class RewardHistoryMan():
    obtained_reward_records: list[RewardRecordItem] = field(default_factory=list)

    def is_eligible(self, reward_enum: RewardEnum):
        info: RewardInfo = reward_enum.value
        category_info: RewardCategoryInfo = info.opts.category.value

        if info.opts.max_claims is not None and info.opts.max_claims < len([rr for rr in self.obtained_reward_records if rr.reward is reward_enum]):
            return False

        if category_info.opts.is_exclusive and len([rr for rr in self.obtained_reward_records if typing.cast(RewardInfo, rr.reward.value).opts.category is info.opts.category]) > 0:
            return False

        return True
    
    def append(self, reward_enum: RewardEnum, consume_id: float):
        self.obtained_reward_records.append(RewardRecordItem(consume_id=consume_id, reward=reward_enum))
        ...
    ...

@dataclass
class InventoryItem():
    remaining_stock: int = 0

@route('奖励系统')
@enable_backup
class Reward(Plugin, InjectNotifier):
    user_histories: UserSpec[RewardHistoryMan] = UserSpec[RewardHistoryMan]()
    inventory: dict[RewardEnum, InventoryItem] = {}

    voucher: Inject['Voucher']
    admin: Inject['Admin']

    def __init__(self):
        self.registed_reward: dict[Plugin, EnumMeta] = {}

    def register(self, plugin: Plugin, em: EnumMeta):
        if plugin in self.registed_reward: return
        self.registed_reward[plugin] = em

    def injected(self, target: Plugin):
        if target in self.registed_reward: return
        mod = sys.modules[target.__module__]
        for _, member in inspect.getmembers(mod, lambda m: inspect.isclass(m) and m.__module__ == mod.__name__):
            if issubclass(member, RewardEnum):
                self.register(target, member)

    def get_remaining_stock(self, reward_enum: RewardEnum):
        info: RewardInfo = reward_enum.value

        if not info.opts.use_inventory_check:
            return
        if reward_enum not in self.inventory:
            return 0
        return self.inventory[reward_enum].remaining_stock

    @delegate()
    async def get_reward(self, reward_enum: RewardEnum, man: RewardHistoryMan):
        info: RewardInfo = reward_enum.value
        is_satisfied = await self.voucher.is_satisfied(cnt=info.opts.ticket_cost)

        if not is_satisfied:
            raise RuntimeError(f'{VOUCHER_NAME}不足, 需要{info.opts.ticket_cost}{VOUCHER_UNIT}')
        
        if not man.is_eligible(reward_enum):
            raise RuntimeError(f'无法兑换奖励, 与已获得的奖励相冲突')
        
        if info.opts.use_inventory_check:
            if reward_enum not in self.inventory:
                self.inventory[reward_enum] = InventoryItem()
            if self.inventory[reward_enum].remaining_stock <= 0:
                raise RuntimeError(f'无法兑换奖励, 库存不足')
        
        self.backup_man.set_dirty()
        record: 'VoucherRecord' = await self.voucher.adjust(
            cnt=Decimal(-info.opts.ticket_cost), 
            extra=VoucherRecordExtraReward(reward=reward_enum)
        )
        man.append(reward_enum, record.id)

        if info.opts.use_inventory_check:
            self.inventory[reward_enum].remaining_stock -= 1

    def aka_to_reward(self, aka: str) -> RewardEnum:
        for meta in self.registed_reward.values():
            e = next((e for e in meta if typing.cast(RewardInfo, e.value).aka == aka), None)
            if e is not None:
                return e
        else:
            raise RuntimeError(f'不存在名叫"{aka}"的奖励')

    @top_instr('设置库存')
    async def set_remaining_stock_cmd(self, aka: str, value: int):
        async with self.admin.privilege(type=AdminType.SUPER):
            reward = self.aka_to_reward(aka)

            info: RewardInfo = reward.value
            if not info.opts.use_inventory_check:
                raise RuntimeError(f'"{aka}"未设置库存检查')
            
            self.backup_man.set_dirty()
            if reward not in self.inventory:
                self.inventory[reward] = InventoryItem()
            self.inventory[reward].remaining_stock = value

            return ['ok']
