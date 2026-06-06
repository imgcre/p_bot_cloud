import asyncio
from decimal import Decimal
import hashlib
import struct
import time
from typing import List, Optional, Union

import cn2an
from mirai import At
from mirai.models.entities import GroupMember

from plugin import Plugin, PathArg, any_instr, delegate, enable_backup, top_instr, InstrAttr, route, Inject
import random
from enum import Enum
from utilities import VOUCHER_NAME, VOUCHER_UNIT, AchvEnum, AchvOpts, AchvRarity, GroupLocalStorage, Source, SourceOp, Upgraded, VoucherRecordExtraAllIn, VoucherRecordExtraMora, throttle_config, voucher_round_half_up
from dataclasses import dataclass, field

import re

from contextlib import asynccontextmanager
from itertools import groupby

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.achv import Achv
    from plugins.throttle import Throttle
    from plugins.voucher import Voucher, VoucherRecord
    from plugins.admin import Admin

class MoraAchv(AchvEnum):
    FIRST_WIN = 0, '首胜', '与bot的猜拳获得首次胜利', AchvOpts(display='✌️')
    CONSECUTIVE_WINS_3 = 1, '三连胜', '与bot的猜拳获得连续三次胜利', AchvOpts(rarity=AchvRarity.UNCOMMON, custom_obtain_msg='运气爆棚', display='👌')
    MINER = 2, '矿工', '与bot猜拳得到的手势签名前24位为0x000000时获得', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='挖到了钻石', display='💎')
    ALL_FOR_NOTHING = 3, '一无所有', f'在抽奖获得{VOUCHER_NAME}后马上通过猜拳输掉', AchvOpts(rarity=AchvRarity.UNCOMMON, custom_obtain_msg='输了个精光!', display='♻️')

class Gesture(Enum):
    Rock = '👊'
    Paper = '✋'
    Scissor = '✌️'

class MoraResult(Enum):
    Draw = '平局'
    PlayerWin = '玩家胜利'
    BotWin = 'bot胜利'

class MoraState(): ...

@dataclass
class MoraStateStarted(MoraState):
    created_ts: int = field(default_factory=time.time_ns)
    bot_gesture: Gesture = field(default_factory=lambda: random.choice([e for e in Gesture]))

    def get_str(self):
        t = bin(int(struct.pack('<Q', self.created_ts).hex(), base=16))[2:].zfill(64).replace('0', '\u200B').replace('1', '\u200C')
        len_t_half = len(t) // 2
        return f'\"{t[:len_t_half]}{self.bot_gesture.value}{t[len_t_half:]}\"'

    def get_digest(self):
        md = hashlib.md5()
        md.update(self.get_str().encode('utf-8'))
        return md.hexdigest()
    
    def is_expired(self):
        return time.time() - self.created_ts / 1e9 > 60 * 5

class AllInState():
    ...

@dataclass
class AllInStateWaitConfirm(AllInState):
    ...

@dataclass
class MoraRecord():
    player_gesture: Gesture
    bot_gesture: Gesture
    result: MoraResult
    created_ts: float = field(default_factory=time.time)

@dataclass
class MoraMan(Upgraded):
    results: List[MoraResult] = field(default_factory=list)
    records: list[MoraRecord] = field(default_factory=list)
    mora_state: Optional[MoraState] = None
    all_in_state: Optional[AllInState] = None

    def play(self, player_gesture: Gesture):
        if isinstance(self.mora_state, MoraStateStarted):
            bot_gesture = self.mora_state.bot_gesture
            bot_gesture_str = self.mora_state.get_str()
            self.mora_state = None
        else:
            bot_gesture = random.choice([e for e in Gesture])
            bot_gesture_str = f'\"{bot_gesture.value}\"'
        
        result = self.determine_winner(player_gesture, bot_gesture)
        # self.results.append(result)
        self.records.append(MoraRecord(
            player_gesture=player_gesture,
            bot_gesture=bot_gesture,
            result=result
        ))
        return result, bot_gesture_str

    @property
    def consecutive_wins(self):
        cnt = 0
        while cnt < len(self.results):
            if self.results[-(cnt+1)] != MoraResult.PlayerWin:
                break
            cnt += 1
        return cnt

    def determine_winner(self, player_gesture: Gesture, computer_gesture: Gesture):
        if player_gesture == computer_gesture:
            return MoraResult.Draw
        elif (player_gesture == Gesture.Rock and computer_gesture == Gesture.Scissor) or \
            (player_gesture == Gesture.Paper and computer_gesture == Gesture.Rock) or \
            (player_gesture == Gesture.Scissor and computer_gesture == Gesture.Paper):
            return MoraResult.PlayerWin
        else:
            return MoraResult.BotWin


@route('mora')
@enable_backup
class RusRou(Plugin):
    gls: GroupLocalStorage[MoraMan] = GroupLocalStorage[MoraMan]()
    achv: Inject['Achv']
    throttle: Inject['Throttle']
    voucher: Inject['Voucher']
    admin: Inject['Admin']
    
    def gesture_from_text(self, gesture: str):
        lookup = {
            '剪|耶|✌️': Gesture.Scissor,
            '石|锤|拳|纳|👊': Gesture.Rock,
            '布|包|✋': Gesture.Paper
        }

        matched_ges = {v for k, v in lookup.items() if re.search(k, gesture) is not None}

        if len(matched_ges) != 1:
            raise RuntimeError(f'{gesture}不是有效的手势')
        
        return matched_ges.pop()

    # https://github.com/Flying9001/stock-alert/blob/master/doc/stock_api_tencent.md
    # http://www.360doc.com/content/23/0928/12/7593676_1098299863.shtml

    @delegate(InstrAttr.FORCE_BACKUP)
    @throttle_config(name='猜拳', max_cooldown_duration=5*60)
    async def do_mora(
        self, 
        mora_man: MoraMan, 
        member: GroupMember,
        source: Source,
        *, 
        gesture: Optional[Union[Gesture, str]] = None,
        cnt: int = 1,
        silent: bool = False,
    ):
        await self.check_voucher(cnt)

        msg = [At(target=member.id)]
        acc = 0

        for i in range(cnt):
            gesture_curr = gesture

            if isinstance(gesture_curr, str):
                gesture_curr = self.gesture_from_text(gesture_curr)

            if gesture_curr is None:
                gesture_curr = random.choice([e for e in Gesture])

            result, bot_gesture_str = mora_man.play(gesture_curr)

            await self.voucher.update_african_chiefs(clean=result==MoraResult.PlayerWin)

            result_str = '平局'

            if result == MoraResult.PlayerWin:
                result_str = f'{VOUCHER_NAME} + 1'
                acc += 1
                await self.voucher.adjust(cnt=Decimal('1'), force=True, extra=VoucherRecordExtraMora(player_win=True))
                await self.achv.submit(MoraAchv.FIRST_WIN)

            if result == MoraResult.BotWin:
                result_str = f'{VOUCHER_NAME} - 1'
                acc -= 1
                await self.voucher.adjust(cnt=Decimal('-1'), force=True, extra=VoucherRecordExtraMora(player_win=False))
                if (await self.voucher.get_count()) == 0:
                    from plugins.voucher import VoucherRecord, VoucherRecordExtraDraw
                    records: list[VoucherRecord] = await self.voucher.get_records()
                    rec = next((rec for rec in reversed(records[:-1]) if rec.count > 0), None)
                    if isinstance(rec.extra, VoucherRecordExtraDraw):
                        await self.achv.submit(MoraAchv.ALL_FOR_NOTHING)
            
            if mora_man.consecutive_wins >= 3:
                await self.achv.submit(MoraAchv.CONSECUTIVE_WINS_3)

            if not silent:
                if cnt <= 1:
                    msg.extend([
                        f' 出了"{gesture_curr.value}", bot 出了{bot_gesture_str} -> {result_str}'
                    ])
                else:
                    if gesture is None:
                        msg.extend([
                            f'\n第{i+1}轮出了"{gesture_curr.value}", bot出了{bot_gesture_str}'
                        ])
                    else:
                        msg.extend([
                            f' {bot_gesture_str}'
                        ])

        if cnt > 1:
            msg.extend([
                f'\n共获得{acc}{VOUCHER_UNIT}{VOUCHER_NAME}' if acc >= 0 else f'\n共失去{-acc}{VOUCHER_UNIT}{VOUCHER_NAME}'
            ])

        await source.op.send(msg)

        if result == MoraResult.Draw and cnt <= 1:
            await asyncio.sleep(1)
            await self.start_progressive()

    @asynccontextmanager
    async def check(self, cnt: Union[int, Decimal]=1):
        await self.check_voucher(cnt=cnt)
        passed = await self.throttle.do_associated(fn=self.do_mora)
        try:
            yield passed
        finally:
            await self.throttle.reset(fn=self.do_mora)
    
    async def check_voucher(self, cnt: Union[int, Decimal] = 1):
        if not await self.voucher.is_satisfied(cnt=cnt):
            raise RuntimeError(f'参与猜拳需要至少持有{cnt}{VOUCHER_UNIT}{VOUCHER_NAME}')
        
    @delegate()
    async def start_progressive(self, member: GroupMember, mora_man: MoraMan, source_op: SourceOp):
        self.backup_man.set_dirty()
        mora_man.mora_state = MoraStateStarted()
        digest_str = mora_man.mora_state.get_digest()[:6].upper()
        if digest_str == '000000':
            await self.achv.submit(MoraAchv.MINER)
        await source_op.send([f'bot已出▩▩▩(md5={digest_str}), 轮到 ', At(target=member.id), ' 了:'])

    @top_instr('猜拳')
    async def start_cmd(self, gesture: Optional[str], mora_man: MoraMan):
        await self.admin.check_proxy(disable_required=True)
        async with self.check() as passed:
            if not passed: return

            if gesture is None:
                await self.start_progressive()
                return
            
            await self.do_mora(gesture=gesture)

    @top_instr('(?P<cnt>.+?)连猜')
    async def start_ten_cmd(self, cnt: PathArg[str], gesture: Optional[str]):
        await self.admin.check_proxy(disable_required=True)
        if any([cnt.startswith(x) for x in ['百', '千', '万', '亿']]):
            cnt = '一' + cnt
        cnt_num = int(cn2an.cn2an(cnt, "smart"))
        if not (cnt_num >= 3):
            return '请至少连猜三次'
        async with self.check(cnt_num) as passed:
            if not passed: return
            
            await self.do_mora(gesture=gesture, cnt=cnt_num, silent=cnt_num > 10)

    @top_instr('梭哈')
    async def start_all_cmd(self, confirm: Optional[str], man: MoraMan):
        await self.admin.check_proxy(disable_required=True)
        async with self.check(cnt=Decimal("0.01")) as passed:
            if not passed: return

            if confirm is None:
                man.all_in_state = AllInStateWaitConfirm()
                self.backup_man.set_dirty()
                return [f'{VOUCHER_NAME}翻倍或者抹零? 回复y确认, 回复其他取消。(手续费1%, 最低0.1券)']
            
            if 'y' in confirm.lower():
                await self.do_all_in()
                return
            else:
                return ['参数错误, 应该为"y"']
    
    @delegate(InstrAttr.FORCE_BACKUP)
    async def do_all_in(self, source: Source):
        cnt: Decimal = await self.voucher.get_count()
        sign = 1 if random.random() < 0.5 else -1
        fee = cnt * Decimal('0.01')
        min_fee = Decimal('0.1')
        if fee < min_fee:
            fee = min_fee
        record: 'VoucherRecord' = await self.voucher.adjust(cnt=sign * cnt - fee, force=True, extra=VoucherRecordExtraAllIn(player_win=sign > 0))
        if sign < 0:
            await self.achv.submit(MoraAchv.ALL_FOR_NOTHING)
        result_str = '获得' if record.count > 0 else '失去'
        win_str = '取得胜利' if sign > 0 else '战败'
        await source.op.send([At(target=source.member.id), f' 在梭哈中{win_str}, 共{result_str}{record.count.copy_abs()}{VOUCHER_UNIT}{VOUCHER_NAME}'])

    @any_instr()
    async def do_all_in_confirm(self, aka: str, man: MoraMan):
        if not isinstance(man.all_in_state, AllInStateWaitConfirm):
            return
        
        man.all_in_state = None
        
        if 'y' in aka.lower():
            await self.do_all_in()
            return
        else:
            return ['取消了梭哈']

    @any_instr()
    async def post_mora(self, aka: str, mora_man: Optional[MoraMan]):
        if aka.startswith('#'): return
        if mora_man is None: return
        if not isinstance(mora_man.mora_state, MoraStateStarted): return
        if mora_man.mora_state.is_expired():
            mora_man.mora_state = None
            self.backup_man.set_dirty()
            return
        
        try:
            gesture = self.gesture_from_text(aka)
        except:
            return

        await self.do_mora(gesture=gesture)


    @top_instr('出(?P<ges>.*?)')
    async def start_initiative(self, gesture: PathArg[str]):
        async with self.check() as passed:
            if not passed: return
        
            await self.do_mora(gesture=gesture)

    @top_instr('猜拳数据')
    async def get_mora_data_cmd(self, mora_man: Optional[MoraMan]):
        if mora_man is None or len(mora_man.results) + len(mora_man.records) == 0:
            return ['还没有进行过猜拳']
        
        results = []

        results.extend(mora_man.results)
        results.extend([rec.result for rec in mora_man.records])
        
        fi_results = [r for r in results if r != MoraResult.Draw]
        fi_results_win = [r for r in fi_results if r == MoraResult.PlayerWin]

        [rec.player_gesture for rec in mora_man.records]

        sorted_recs = sorted(mora_man.records, key=lambda rec: rec.player_gesture.value)

        most_like_gesture_pk = next(iter(sorted(
            [(k, (len(li := sorted(list(v), key=lambda it: it.created_ts)) + (1 - 1 / li[-1].created_ts))) for k, v in groupby(sorted_recs, key=lambda rec: rec.player_gesture)],
            key=lambda it: it[1],
            reverse=True
        )), None)

        res = [
            f'一共猜过{len(results)}次', 
            f'胜率为{len(fi_results_win) / len(fi_results) * 100 if len(fi_results) > 0 else 0:.1f}%',
        ]

        if most_like_gesture_pk is not None:
            res.extend([
                f'最爱出{most_like_gesture_pk[0].value}'
            ])

        most_like_gesture_when_loss_pk = next(iter(sorted(
            [(k, (len(li := sorted(list(v), key=lambda it: it.created_ts)) + (1 - 1 / li[-1].created_ts))) for k, v in groupby([rec for rec in sorted_recs if rec.result == MoraResult.BotWin], key=lambda rec: rec.player_gesture)],
            key=lambda it: it[1],
            reverse=True
        )), None)

        grouped = groupby(sorted_recs, key=lambda rec: rec.player_gesture)
        scored: list[tuple[Gesture, float, float, int]] = []

        for k, _v in grouped:
            v = list(_v)
            if not v:
                continue  # 防止空组

            li = sorted(v, key=lambda it: it.created_ts)
            bot_win_count = len([rec for rec in v if rec.result == MoraResult.BotWin])
            win_or_loss_count = len([rec for rec in v if rec.result != MoraResult.Draw])
            
            # 防止除以0
            if not li or li[-1].created_ts == 0:
                continue

            # score = (bot_win_count / len(li)) + (1 - 1 / li[-1].created_ts)
            scored.append((k, bot_win_count / win_or_loss_count, 1 - 1 / li[-1].created_ts))

        # 选出得分最高的手势
        most_like_gesture_when_loss_pk_2 = next(iter(
            sorted(scored, key=lambda it: it[1], reverse=True)
        ), None)

        if most_like_gesture_when_loss_pk_2 is not None:
            res.extend([
                f'常因为出{most_like_gesture_when_loss_pk_2[0].value}而输掉({(most_like_gesture_when_loss_pk_2[1]) * 100:.1f}%)'
            ])
        return ['\n'.join(res)]
