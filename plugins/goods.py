
from dataclasses import dataclass, field
import time
import typing
from uuid import UUID
import uuid

from mirai import At
from mirai.models.entities import Group
from plugin import InstrAttr, Plugin, route, top_instr, Inject
from utilities import VOUCHER_NAME, VOUCHER_UNIT, AdminType, RewardCategoryEnum, RewardEnum, RewardInfo, RewardOpts, Upgraded, UserSpec

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from plugins.reward import Reward
    from plugins.admin import Admin

class GoodsRewardCategories(RewardCategoryEnum):
    _ = 0, '普通物料'

class GoodsReward(RewardEnum):
    REFRIGERATOR_MAGNET = 0, '冰箱贴', RewardOpts(
        category=GoodsRewardCategories._,
        max_claims=1,
        ticket_cost=3,
        use_inventory_check=True,
        desc='包邮'
    )

    FUR_JAR = 1, '猫毛罐头', RewardOpts(
        category=GoodsRewardCategories._,
        max_claims=1,
        ticket_cost=10,
        use_inventory_check=True,
        desc='包邮,盲盒'
    )

    MINI_PILLOW = 2, '小方枕', RewardOpts(
        category=GoodsRewardCategories._,
        max_claims=1,
        ticket_cost=98,
        use_inventory_check=True,
        desc='包邮'
    )

    NA_PLUSS = 9999, '纳延', RewardOpts(
        category=GoodsRewardCategories._,
        max_claims=1,
        ticket_cost=10000,
        use_inventory_check=True,
        desc='限时特惠'
    )

class GoodsState(): ...

@dataclass
class GoodsStatePending(GoodsState):
    created_ts: float = field(default_factory=time.time)

@dataclass
class GoodsStateCompleted(GoodsState):
    created_ts: float = field(default_factory=time.time)

@dataclass
class AcquiredGoodsItem(Upgraded):
    reward: RewardEnum
    id: UUID = field(default_factory=uuid.uuid4)
    state: GoodsState = field(default_factory=GoodsStatePending)

@dataclass
class GoodsMan():
    acquired_goods: list[AcquiredGoodsItem] = field(default_factory=list)

@route('物料')
class Goods(Plugin):
    user_histories: UserSpec[GoodsMan] = UserSpec[GoodsMan]()
    reward: Inject['Reward']
    admin: Inject['Admin']

    @top_instr('所有物料|库存')
    async def all_goods_cmd(self):
        return ['\n'.join([
            f'{(info := typing.cast(RewardInfo, e.value)).aka}{f"({info.opts.desc})" if info.opts.desc is not None else ""} 需{info.opts.ticket_cost}{VOUCHER_UNIT}{VOUCHER_NAME} 剩{st if (st := self.reward.get_remaining_stock(e)) is not None else "很多"}个' for e in GoodsReward
        ])]
    
    @top_instr('我的物料|背包')
    async def my_goods_cmd(self, man: Optional[GoodsMan]):
        if man is None or len(man.acquired_goods) == 0:
            return ['尚未获得任何物料']
        
        return ['\n'.join([f'{typing.cast(RewardInfo, it.reward.value).aka}{" (待发货)" if isinstance(it.state, GoodsStatePending) else ""}' for it in man.acquired_goods])]

    @top_instr('兑换(物料)?')
    async def claim_reward(self, aka: str, man: GoodsMan):
        reward = self.reward.aka_to_reward(aka)
        await self.reward.get_reward(reward)
        self.backup_man.set_dirty()
        man.acquired_goods.append(AcquiredGoodsItem(reward))
        return [f'成功兑换"{aka}"*1']
    
    @top_instr('待发货', InstrAttr.NO_ALERT_CALLER)
    async def query_pending_goods(self, group: Group):
        li = []
        for user_id, man in self.user_histories.users.items():
            pending_goods = [
                f'  {typing.cast(RewardInfo, goods.reward.value).aka}({goods.id.hex[:6].upper()})' 
                for goods in man.acquired_goods 
                if isinstance(goods.state, GoodsStatePending)
            ]

            if len(pending_goods) > 0:
                target_member = await self.bot.get_group_member(group, user_id)
                li.extend([f'{target_member.member_name if target_member is not None else "?已退群?"}({user_id}):'])
                li.extend(pending_goods)
        
        def intersperse(lst, item):
            result = [item] * (len(lst) * 2 - 1)
            result[0::2] = lst
            return result
        
        return intersperse(li, '\n')

    @top_instr('确认发货', InstrAttr.NO_ALERT_CALLER)
    async def set_goods_complete(self, id_prefix: str):
        async with self.admin.privilege(type=AdminType.SUPER):
            for user_id, man in self.user_histories.users.items():
                for goods in man.acquired_goods:
                    if not isinstance(goods.state, GoodsStatePending):
                        continue
                    if not goods.id.hex.upper().startswith(id_prefix.upper()):
                        continue
                    goods.state = GoodsStateCompleted()
                    self.backup_man.set_dirty()
                    return ['由', At(target=user_id), f' 兑换的物料{typing.cast(RewardInfo, goods.reward.value).aka}({goods.id.hex[:6].upper()})已发货']
            return [f'没有找到编号为{id_prefix}的物料']