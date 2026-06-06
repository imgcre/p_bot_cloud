from dataclasses import dataclass, field
import time
from typing import Final, Optional
import typing

from mirai import Image

from plugin import Inject, Plugin, delegate, enable_backup, top_instr, any_instr, InstrAttr, route
import random
import random
from enum import Enum
from itertools import groupby

from utilities import AchvEnum, AchvInfo, AchvRarity, AchvRarityVal, UserSpec

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.achv import Achv

class Reward(Enum):
    BALL_OF_YARN = '毛线球'
    WALLOW = '打滚'
    PALM_TREASURE = '掌中宝'
    ELIZABETHAN_COLLAR = '伊丽莎白圈'
    PASSION_FRUIT = '百香果'
    ...

@dataclass
class DrawResult():
    consumed_achv: AchvEnum
    reward: Optional[Reward]
    create_ts: float = field(default_factory=time.time)
    put_back: bool = False

@dataclass
class UserSweepstakeMan():
    results: list[DrawResult] = field(default_factory=list)
    in_flow: bool = False

    def append_result(self, res: DrawResult):
        self.results.append(res)
    
    def get_rewards(self):
        return [r.reward for r in self.results if r.reward is not None and not r.put_back]
    
    def clear_rewards(self):
        rewards = self.get_rewards()
        for r in self.results:
            if r.reward is not None:
                r.put_back = True
        return rewards
        ...


@route('抽barchi')
@enable_backup
class Sweepstake(Plugin):
    user_sweepstakes: UserSpec[UserSweepstakeMan] = UserSpec[UserSweepstakeMan]()

    achv: Inject['Achv']

    prize_pool: list[Reward] = [*[Reward.BALL_OF_YARN] * 6, *[Reward.WALLOW] * 4, *[Reward.PALM_TREASURE] * 5, *[Reward.ELIZABETHAN_COLLAR] * 5, *[Reward.PASSION_FRUIT] * 6]

    # 毛线球: 6
    # 粉色 4
    # 掌中宝 5
    # 伊丽莎白圈 5
    # 百香果 6

    PROBS: Final = {
        AchvRarity.UNCOMMON: 0.1,
        AchvRarity.RARE: 0.3,
        AchvRarity.EPIC: 0.7,
        AchvRarity.LEGEND: 0.99,
    }

    MAG_PUNISHMENT: Final = 0.01 # 惩罚倍率

    @delegate(InstrAttr.FORCE_BACKUP)
    async def bar_chi(self, aka: str, man: UserSweepstakeMan):

        obtained_rewards = man.get_rewards()

        if len(obtained_rewards) > 0:
            return f'已获得奖励: {", ".join([r.value for r in obtained_rewards])}, 无需重复抽取', True
        
        if len(self.prize_pool) == 0:
            return '所有的吧唧都已经抽完啦', True
            
        ac: AchvEnum = await self.achv.aka_to_achv(aka)
        ac_info = typing.cast(AchvInfo, ac.value)
        rarity = ac_info.opts.rarity
        if rarity not in self.PROBS:
            return f'成就{ac_info.aka}不可参与该活动', False
        
        has_ac = await self.achv.has(ac)
        if not has_ac:
            return f'尚未获得成就{ac_info.aka}', False
        
        prob = self.PROBS[rarity]
 
        if ac_info.opts.is_punish:
            prob *= self.MAG_PUNISHMENT

        val = random.random()

        outputs = []

        await self.achv.remove(ac)
        outputs.append(f'消耗了{ac_info.aka}...')

        outputs.append(f'将以{prob * 100:.1f}%的概率进行抽奖...')
        outputs.append(f'投掷得到了数值: {val:.3f}...')

        if val > prob:
            man.append_result(DrawResult(consumed_achv=ac, reward=None))
            outputs.append(f'失败, 未获得奖励')
            return '\n'.join(outputs), False

        reward = self.prize_pool.pop(random.randrange(len(self.prize_pool)))
        man.append_result(DrawResult(consumed_achv=ac, reward=reward))
        outputs.append(f'成功获得奖励{reward.value}')
        return '\n'.join(outputs), True

        ...

    @any_instr()
    async def in_flow_bar_chi_cmd(self, aka: str, man: UserSweepstakeMan):
        if not man.in_flow: return

        if aka.startswith('#'):
            self.backup_man.set_dirty()
            man.in_flow = False
            return

        try:
            ret, ex = await self.bar_chi(aka)
            if ex:
                self.backup_man.set_dirty()
                man.in_flow = False
            return ret
        except Exception as e:
            self.backup_man.set_dirty()
            man.in_flow = False
            return [
                f' 退出了连续抽奖模式, 原因: ',
                *e.args
            ]
        
    @any_instr()
    async def in_flow_bar_chi_img_exit(self, _: Image, man: UserSweepstakeMan):
        if not man.in_flow: return

        self.backup_man.set_dirty()
        man.in_flow = False

    @top_instr('放回吧唧', InstrAttr.FORCE_BACKUP)
    async def back_bar_chi_cmd(self, man: UserSweepstakeMan):
        rewards = man.clear_rewards()
        if len(rewards) == 0:
            return ' 尚未获得任何吧唧'
        self.prize_pool.extend(rewards)
        return f' 向奖池中放回了{", ".join([r.value for r in rewards])}'
        ...

    # @admin
    # @top_instr('吧唧战绩', InstrAttr.NO_ALERT_CALLER)
    # async def bar_chi_history_cmd(self, who: At):
    #     man = self.user_sweepstakes.get_data(who.target)
    #     if man is None:
    #         return '未进行过抽奖活动'
        
    #     li = []

    #     for r in man.results:
    #         tz = pytz.timezone('Asia/Shanghai')
    #         dt_object = datetime.fromtimestamp(r.create_ts, tz=tz)
    #         line = [f'在{dt_object.strftime("%m-%d %H:%M:%S")}消耗了{r.consumed_achv.value.aka}']
    #         if r.reward is None:
    #             line.append('什么都没有获得')
    #         else:
    #             line.append(f'获得了{r.reward.value}')
            
    #         li.append(', '.join(line))
            
    #     if len(li) == 0:
    #         return '未进行过抽奖活动'
        
    #     return '\n'.join(li)

    @top_instr('吧唧库存')
    async def bar_chi_remians_cmd(self):
        barchis_sorted = sorted(self.prize_pool, key=lambda it: it.name)
        barchis_groups = groupby(barchis_sorted, lambda it: it)

        d = {}

        for k, v in barchis_groups:
            d[k] = len(list(v))

        r = []

        for r_enum in Reward:
            r.append(f'{r_enum.value}: {d.get(r_enum, 0)}')
        
        return '\n'.join(r)

    @top_instr('抽吧唧', InstrAttr.FORCE_BACKUP)
    async def bar_chi_cmd(self, aka: Optional[str], man: UserSweepstakeMan):

        if aka is None:
            prob_texts = [f'{typing.cast(AchvRarityVal, k.value).aka}: {v * 100:.0f}%' for k, v in self.PROBS.items()]

            obtained_achvs: list[AchvInfo] = [typing.cast(AchvInfo, a.value) for a in await self.achv.get_obtained()]
            filtered_achvs = [a for a in obtained_achvs if a.opts.rarity in self.PROBS.keys()]

            def comp(it: AchvInfo):
                return typing.cast(AchvRarityVal, it.opts.rarity.value).level

            sorted_achvs: list[AchvInfo] = sorted(filtered_achvs, key=comp)
            grouped = groupby(sorted_achvs, lambda it: it.opts.rarity)

            avaliable_achvs = [f'[{typing.cast(AchvRarityVal, k.value).aka} {self.PROBS[k] * 100:.0f}%] {", ".join([a.aka for a in g])}' for k, g in grouped]
            if len(avaliable_achvs) == 0:
                avaliable_achvs = ['暂时还没有, 请继续保持努力哦']
            else:
                man.in_flow = True

            example_achv_aka = next((a.aka for a in filtered_achvs), '连五鞭')

            return '\n'.join([
                '消耗一个成就抽取吧唧物料🐱🐾', 
                f'抽奖方式: 请直接发送<成就名>进行连续抽奖, 如发送 {example_achv_aka}',
                '', 
                # '获奖概率：', 
                # *prob_texts, 
                # f'*惩罚倍率: {self.MAG_PUNISHMENT * 100:.0f}%',
                # '',
                '可用于抽奖的成就: ',
                *avaliable_achvs
            ])
        
        man.in_flow = False
        ret, ex = await self.bar_chi(aka)
        return ret

 