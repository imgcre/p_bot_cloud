from dataclasses import dataclass, field
import time
from typing import Any, Final, Optional
import typing
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID
import uuid

from mirai import At, Image
from mirai.models.entities import GroupMember, Group
import mirai.models.message

from nap_cat_types import GetGroupMemberInfoResp
from plugin import Inject, Plugin, delegate, enable_backup, top_instr, any_instr, InstrAttr, route
import random
import random
from itertools import groupby

from utilities import VOUCHER_NAME, VOUCHER_UNIT, AchvEnum, AchvInfo, AchvOpts, AchvRarity, AchvRarityVal, AdminType, Source, Upgraded, User, UserSpec, VoucherRecordKill, VoucherRecordReward, voucher_round_half_up

class VoucherAchv(AchvEnum):
    AFRICAN_CHIEFS = 0, '非酋', '连续100次抽奖都未成功', AchvOpts(rarity=AchvRarity.LEGEND, display='🧔🏿', custom_obtain_msg='成为了反方向的欧皇', target_obtained_cnt=100)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.achv import Achv
    from plugins.fur import Fur
    from plugins.nap_cat import NapCat
    from plugins.admin import Admin
    from plugins.renderer import Renderer

@dataclass
class DrawResult():
    consumed_achv: AchvEnum
    suceeed: bool
    create_ts: float = field(default_factory=time.time)

@dataclass
class ConsumeRecord():
    id: float
    count: Decimal
    create_ts: float = field(default_factory=time.time)

@dataclass
class GiftRecord():
    consume_id: float
    count: Decimal
    create_ts: float = field(default_factory=time.time)
    ...


@dataclass
class VoucherRecord():
    count: Decimal
    extra: Optional[Any]
    id: UUID = field(default_factory=uuid.uuid4)
    create_ts: float = field(default_factory=time.time)
    ...

@dataclass
class VoucherRecordExtraDraw():
    consumed_achv: AchvEnum

@dataclass
class VoucherRecordExtraGiveaway():
    to_id: int
    transaction_id: UUID

@dataclass
class VoucherRecordExtraReceive():
    from_id: int
    transaction_id: UUID


@dataclass
class UserVoucherMan(Upgraded):
    results: list[DrawResult] = field(default_factory=list)
    consumes: list[ConsumeRecord] = field(default_factory=list)
    gifts: list[GiftRecord] = field(default_factory=list)
    in_flow: bool = False
    count: Decimal = 0

    records: list[VoucherRecord] = field(default_factory=list)

    factor: Optional[Decimal] = field(default_factory=lambda: Decimal('1'))

    # def append_result(self, res: DrawResult):
    #     self.results.append(res)

    def submit_record(self, record: VoucherRecord):
        self.records.append(record)
        self.count += record.count
        self.count = voucher_round_half_up(self.count)

    # def append_consume(self, cnt: Decimal):
    #     while True:
    #         n_float = random.random()
    #         same_id_item = next((r.id for r in self.consumes if r.id == n_float), None)
    #         if same_id_item is not None:
    #             continue
    #         self.consumes.append(ConsumeRecord(id=n_float, count=cnt))
    #         return n_float
        
    # def append_gift(self, rec: GiftRecord):
    #     self.results.append(rec)
    #     self.count += rec.count

    def is_satisfied(self, cnt: Decimal):
        if cnt < 0: return True
        return self.count >= cnt
    
    def get_count(self):
        return self.count

    
# class BarchiRewardCategories(RewardCategoryEnum):
#     _ = 0, '吧唧'

# class Barchis(RewardEnum):
#     ALL_OF_YARN = 0, '毛线球', RewardOpts(category=BarchiRewardCategories._)



# 类别的is_exclusive为True的话, RewardOpts的max_claims将强制为1
    
# class RewardCategories(Enum):
#     Barchi = '吧唧', RewardCategoryOpts(is_exclusive=True)
#     ...

# class Barchis(Enum):
#     ALL_OF_YARN = '毛线球', RewardOpts(category=RewardCategories.Barchi, max_claims=1, ticket_cost=1)
#     WALLOW = '打滚'
#     PALM_TREASURE = '掌中宝'
#     ELIZABETHAN_COLLAR = '伊丽莎白圈'
#     PASSION_FRUIT = '百香果'

@route('兑奖券系统')
@enable_backup
class Voucher(Plugin):
    user_sweepstakes: UserSpec[UserVoucherMan] = UserSpec[UserVoucherMan]()

    achv: Inject['Achv']
    fur: Inject['Fur']
    nap_cat: Inject['NapCat']
    admin: Inject['Admin']
    renderer: Inject['Renderer']

    PROBS: Final = {
        AchvRarity.UNCOMMON: Decimal('0.1'),
        AchvRarity.RARE: Decimal('0.3'),
        AchvRarity.EPIC: Decimal('0.7'),
        AchvRarity.LEGEND: Decimal('0.99'),
    }

    MAG_PUNISHMENT: Final = Decimal('0.01') # 惩罚倍率

    @delegate()
    async def is_satisfied(self, user: User, *, cnt: Decimal):
        man = self.user_sweepstakes.get_data(user.id)
        if man is None:
            return False
        return man.is_satisfied(cnt)
    
    @delegate()
    async def check_satisfied(self, *, cnt: Decimal):
        if not await self.is_satisfied(cnt=cnt):
            raise RuntimeError(f'{VOUCHER_NAME}不足, 需{cnt}{VOUCHER_UNIT}')
        ...
    
    @delegate()
    async def adjust(self, user: User, *, cnt: Decimal, force: bool=False, extra: Optional[Any]=None):
        cnt = voucher_round_half_up(cnt)

        if not force:
            await self.check_satisfied(cnt=-cnt)
        
        self.backup_man.set_dirty()
        man = self.user_sweepstakes.get_or_create_data(user.id)
        record = VoucherRecord(count=cnt, extra=extra)
        man.submit_record(record)
        return record

    # @delegate()
    # async def consume(self, user: User, *, cnt: Decimal, force: bool=False, note: Optional[str]=None):
    #     if not force:
    #         await self.check_satisfied(cnt=cnt)
        
    #     self.backup_man.set_dirty()
    #     man = self.user_sweepstakes.get_or_create_data(user.id)
    #     man.count -= cnt
    #     id = uuid.uuid4()
    #     man.append_record(VoucherRecordGeneral(id=id, count=-cnt, note=note))
    #     return id

    async def update_african_chiefs(self, *, clean: bool=False, rarity: Optional[AchvRarity]=None):
        if clean:
            if not await self.achv.has(VoucherAchv.AFRICAN_CHIEFS):
                await self.achv.remove(VoucherAchv.AFRICAN_CHIEFS, force=True)
        else:
            if rarity is AchvRarity.LEGEND:
                await self.achv.submit(
                    VoucherAchv.AFRICAN_CHIEFS, 
                    override_obtain_cnt=VoucherAchv.AFRICAN_CHIEFS.opts.target_obtained_cnt-1
                )
            else:
                await self.achv.submit(VoucherAchv.AFRICAN_CHIEFS)

    # 输出文本, 退出持续模式
    @delegate(InstrAttr.FORCE_BACKUP)
    async def draw(self, aka: str, man: UserVoucherMan, source: Source):

        try:
            ac: AchvEnum = await self.achv.aka_to_achv(aka)
        except Exception as e:
            try:
                return await self.fur.get_pic(aka), True
            except:
                raise e
     
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

        prob *= man.factor

        val = random.random()

        outputs = []

        await self.achv.remove(ac)
        outputs.append(f'消耗了{ac_info.aka}...')

        if man.factor != Decimal('1'):
            outputs.append(f'当前的概率因子: {man.factor:f}')

        outputs.append(f'将以{prob * 100:.1f}%的概率进行抽奖...')
        outputs.append(f'投掷得到了数值: {val:.3f}...')

        if val > prob:
            # man.append_result(DrawResult(consumed_achv=ac, suceeed=False))

            man.submit_record(VoucherRecord(
                count=Decimal('0'),
                extra=VoucherRecordExtraDraw(
                    consumed_achv=ac
                )
            ))
            outputs.append(f'失败, 未获得奖励')
            await self.update_african_chiefs()
            return '\n'.join(outputs), False

        # man.append_result(DrawResult(consumed_achv=ac, suceeed=True))
        man.submit_record(VoucherRecord(
                count=Decimal('1'),
                extra=VoucherRecordExtraDraw(
                    consumed_achv=ac
                )
            ))
        outputs.append(f'成功获得了{VOUCHER_NAME}*1')
        await self.update_african_chiefs(clean=True)

        msg_id = source.get_message_id()
        if msg_id is not None:
            await self.nap_cat.set_msg_emoji_like(msg_id, 320)

        return '\n'.join(outputs), True

    @any_instr()
    async def in_flow_draw_cmd(self, aka: str, man: UserVoucherMan):
        if not man.in_flow: return

        if aka.startswith('#'):
            self.backup_man.set_dirty()
            man.in_flow = False
            return

        try:
            ret, ex = await self.draw(aka)
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
    async def in_flow_draw_img_exit(self, _: Image, man: UserVoucherMan):
        if not man.in_flow: return

        self.backup_man.set_dirty()
        man.in_flow = False

    @delegate()
    async def get_count(self, man: Optional[UserVoucherMan]):
        cnt = Decimal('0')
        if man is not None:
            cnt = man.count

        if not isinstance(cnt, Decimal):
            cnt = Decimal(cnt)
        return cnt
    
    @delegate()
    async def get_records(self, man: Optional[UserVoucherMan]):
        if man is None:
            return []
        return man.records
        ...

    @delegate(InstrAttr.FORCE_BACKUP)
    async def set_prob_factor(self, man: UserVoucherMan, *, factor: Decimal):
        prev_factor = man.factor
        man.factor = factor
        return prev_factor

    @top_instr('概率因子')
    async def set_prob_factor_cmd(self, at: At, factor_str: str):
        async with self.admin.privilege(type=AdminType.SUPER):
            factor = Decimal(factor_str)
            member = await self.member_from(at=at)
            async with self.override(member):
                prev_factor: Decimal = await self.set_prob_factor(factor=factor)
            
            changed_rate = factor / prev_factor

            adj_str = f'降低{1 / changed_rate:f}倍' if factor < 1 else f'提高{changed_rate:f}倍'
            return ['已将', at, f'的获奖概率{adj_str}']

    @top_instr('回滚', InstrAttr.FORCE_BACKUP)
    async def rollback_cmd(self):
        async with self.admin.privilege(type=AdminType.SUPER):
            
            ...
        ...

    @top_instr('富豪榜', InstrAttr.NO_ALERT_CALLER)
    async def get_rich_list_cmd(self, group: Group):
        members = (await self.bot.member_list(group.id)).data

        li_all = sorted(
            [
                (member, man) 
                for user_id, man in self.user_sweepstakes.users.items() 
                if (member := next((member for member in members if member.id == user_id), None)) is not None and man.count > 0
            ],
            key=lambda it: it[1].count,
            reverse=True
        )

        all_count = sum([man.count for member, man in li_all], Decimal('0'))

        richs = []
        data = {
            'total': str(all_count),
            'richs': richs,
            'total_member': len(li_all),
        }

        if len(li_all) == 0:
            return [f'大家都没有{VOUCHER_NAME}……']
        
        curr_sum = Decimal('0')
        
        # res = [f'市面上一共流通了{all_count}张券\n']

        for member, man in li_all:
            async with self.override(member):
                richs.append({
                    'name': await self.achv.get_raw_member_name(),
                    'avatar_url': '',
                    'value': str(man.count),
                })
                curr_sum += man.count
                if curr_sum / all_count > 0.9:
                    break
            

        b64_img = await self.renderer.render('rich-list', data=data)

        return [
            mirai.models.message.Image(base64=b64_img)
        ]

    # @top_instr('富豪榜-old', InstrAttr.NO_ALERT_CALLER)
    # async def get_rich_list_old_cmd(self, group: Group):
    #     members = (await self.bot.member_list(group.id)).data

    #     li_all = sorted(
    #         [
    #             (member, man) 
    #             for user_id, man in self.user_sweepstakes.users.items() 
    #             if (member := next((member for member in members if member.id == user_id), None)) is not None and man.count > 0
    #         ],
    #         key=lambda it: it[1].count,
    #         reverse=True
    #     )

    #     all_count = sum([man.count for member, man in li_all])

    #     li = li_all[:3]

    #     if len(li) == 0:
    #         return ['大家都没有券券……']
        
    #     res = [f'市面上一共流通了{all_count}张券\n']

    #     for member, man in li:
    #         async with self.override(member):
    #             res.append(f'{await self.achv.get_raw_member_name()}({member.id}): 有{man.count}张券 占总量{man.count / all_count * 100:.1f}%')

    #     return '\n'.join(res)

    @top_instr(f'兑奖券|兑换券|{VOUCHER_NAME}')
    async def get_ticket_cnt_cmd(self):
        cnt: Decimal = await self.get_count()

        tx = [f'你当前共持有{cnt}{VOUCHER_UNIT}{VOUCHER_NAME}']

        if cnt < 1:
            tx.append(f'\n\n小贴士：可以通过以下方式获取{VOUCHER_NAME}\n获取成就并使用【#抽奖 <成就名>】指令进行抽奖，成就的稀有度会影响抽得{VOUCHER_NAME}的概率，使用指令【#说明】以了解更多\n在毛播间观看直播时，若您的头像气泡上方出现宝箱气泡，投喂"小花花"有机会获得0.1到10{VOUCHER_UNIT}{VOUCHER_NAME}')

        return tx
    
    @top_instr('赠送')
    async def give_ticket(self, at: Optional[At], me: GroupMember):
        await self.admin.check_proxy(disable_required=True)
        # info: MemberInfoModel = await self.bot.member_info(me.group.id, me.id).get()
        info: GetGroupMemberInfoResp = await self.nap_cat.get_group_member_info()
        if info is None:
            return ['无法获取成员信息']

        if int(info.level) < 50:
            return ['赠送失败: 等级过低']

        if at is None:
            return ['请指定赠予对象']

        await self.check_satisfied(cnt=Decimal('1.1'))

        self.backup_man.set_dirty()

        man_from = self.user_sweepstakes.get_or_create_data(me.id)
        man_to = self.user_sweepstakes.get_or_create_data(at.target)

        transaction_id = uuid.uuid4()

        man_from.submit_record(VoucherRecord(
            count=Decimal('-1.1'),
            extra=VoucherRecordExtraGiveaway(
                to_id=at.target,
                transaction_id=transaction_id
            )
        ))

        man_to.submit_record(VoucherRecord(
            count=Decimal('1'),
            extra=VoucherRecordExtraReceive(
                from_id=me.id,
                transaction_id=transaction_id
            )
        ))

        return [' 向', at, f' 赠送了一{VOUCHER_UNIT}{VOUCHER_NAME}']

    @top_instr('抽奖', InstrAttr.FORCE_BACKUP)
    async def draw_cmd(self, aka: Optional[str], man: UserVoucherMan):
        await self.admin.check_proxy(disable_required=True)
        if aka is None:
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
                f'消耗一个成就抽取{VOUCHER_NAME}🐱🐾', 
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
        ret, ex = await self.draw(aka)
        return ret

 