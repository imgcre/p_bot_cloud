from dataclasses import dataclass, field
from decimal import Decimal
import re
import time
from typing import Optional
from plugin import AchvCustomizer, InstrAttr, Plugin, any_instr, delegate, enable_backup, route, top_instr, Inject
from py_mini_racer import MiniRacer
import aiohttp
from utilities import VOUCHER_NAME, VOUCHER_UNIT, AchvEnum, AchvOpts, AchvRarity, GroupLocalStorage, SourceOp, VoucherRecordExtraStock, throttle_config, voucher_round_half_up
from py_mini_racer.py_mini_racer import JSEvalException
from datetime import datetime

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.voucher import Voucher, VoucherRecord
    from plugins.throttle import Throttle
    from plugins.achv import Achv

class StockAchv(AchvEnum):
    MOUTAI = 0, '酱香科技', '买入一股茅台的股票', AchvOpts(rarity=AchvRarity.LEGEND, display='🍾', custom_obtain_msg='正在研究酱香科技', dynamic_deletable=True)

# 统一用search得到的股票代码

# 需要提供总收益的计算

# 买入时间戳 买入数量 单价

@dataclass
class StockHistoryItem():
    quantity_delta: int # 正值买入, 负值卖出
    price: Decimal # 单价
    created_ts: float = field(default_factory=time.time)

@dataclass
class UserStock():
    cnt: int = 0 # 当前持有的数量
    history: list[StockHistoryItem] = field(default_factory=list)

    # 显示收益百分比和当前总价格

    # 台积电(TSM)x1 2.5券 +8.4%

    def total_cost(self) -> Decimal:
        """计算当前持仓成本总额"""
        total_quantity = 0
        total_cost = Decimal('0')

        for item in self.history:
            if item.quantity_delta > 0:
                total_quantity += item.quantity_delta
                total_cost += item.price * item.quantity_delta
            elif item.quantity_delta < 0:
                # 卖出时，按比例减少总成本
                if total_quantity > 0:
                    total_cost = total_cost * Decimal(total_quantity + item.quantity_delta) / Decimal(total_quantity)
                total_quantity += item.quantity_delta
                
                if total_quantity <= 0:
                    total_quantity = 0
                    total_cost = Decimal('0')

        if self.cnt == 0:
            return Decimal('0')

        return total_cost

    def average_cost(self) -> Decimal:
        """当前持有股票的平均成本"""
        if self.cnt == 0:
            return Decimal('0')
        return self.total_cost() / self.cnt

    def calc_profit(self, current_price: Decimal) -> Decimal:
        """当前总浮动收益"""
        return current_price * self.cnt - self.total_cost()

    def calc_profit_percent(self, current_price: Decimal) -> Decimal:
        """当前收益率（百分比）"""
        cost = self.total_cost()
        if cost == 0:
            return Decimal('0')
        return self.calc_profit(current_price) / cost * 100

    def display_summary(self, stock_info: 'StockData') -> str:
        profit_pct = self.calc_profit_percent(stock_info.current_price)
        profit_sign = "+" if profit_pct >= 0 else "-"
        return f"{stock_info.name}({stock_info.region_code})x{self.cnt} {stock_info.current_price:.2f}{VOUCHER_UNIT}{VOUCHER_NAME} {profit_sign}{abs(profit_pct):.1f}%"


    ...

@dataclass
class SotckInfo():
    name: str
    code: str

# 200 美股, 100 港股, 51 深圳, 1 上海
@dataclass
class StockData():
    region_code: str
    name: str
    current_price: Decimal # 当前股价

class StockApi():
    @staticmethod
    async def get_stock_data(r_codes: list[str]) -> dict[str, StockData]:
        code_map = {re.sub(r'^us\.?(\w+)(?:\.\w+)?$', r'us\1', c): c for c in r_codes}
        

        api_url = f'https://sqt.gtimg.cn/q={",".join(code_map.keys())}'
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(api_url) as response:
                js_res = await response.text(encoding='gb2312')

        ctx = MiniRacer()
        ctx.eval(js_res)

        res = {}

        for c in code_map.keys():
            try:
                arr = ctx.eval(f'v_{c}').split('~')
                print(arr)

                res[code_map[c]] = StockData(
                    region_code=arr[0],
                    name=arr[1],
                    current_price=Decimal(arr[3]) * Decimal('0.1'),
                )
            except JSEvalException:
                print(f'v_{c} not defined')
            

        return res

    @classmethod
    async def search(cls, q: str) -> Optional[SotckInfo]:
        try:
            api_url = f'https://proxy.finance.qq.com/cgi/cgi-bin/smartbox/search?stockFlag=1&fundFlag=1&app=official_website&query={q}'
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(api_url) as response:
                    j = await response.json(encoding='utf-8')
            first_match = j['stock'][0]

            if first_match['reportInfo']['match_level'] != 'full_match':
                return
            
            return SotckInfo(
                name=first_match['name'],
                code=first_match['code']
            )
        except:
            ...

    @classmethod
    def code_to_search_q(cls, code: str):
        return re.sub(r'^[a-z]+\.?(\w+)(?:\.\w+)?$', r'\1', code)


class ChatState():
    ...

@dataclass
class ChatStateIdle(ChatState):
    ...

@dataclass
class ChatStateBuyConfirming(ChatState):
    code: str
    cnt: int
    price: Decimal # 单价
    ...

@dataclass
class StockMan():
    held_stocks: dict[str, UserStock] = field(default_factory=dict)
    chat_state: ChatState = field(default_factory=ChatStateIdle)

    def adjust(self, code: str, cnt: int, price: Decimal):
        if code not in self.held_stocks:
            self.held_stocks[code] = UserStock()

        # TODO 判断透支
        if self.held_stocks[code].cnt < -cnt:
            raise RuntimeError('股票数量不足, 无法完成交易')
        
        self.held_stocks[code].cnt += cnt
        self.held_stocks[code].history.append(StockHistoryItem(
            quantity_delta=cnt,
            price=price # 单价
        ))

    def is_satisfied(self, code: str, cnt: int):
        return self.held_stocks[code].cnt >= cnt

    def has(self, code: str):
        return code in self.held_stocks

    async def summary(self) -> list[str]:
        data = await StockApi.get_stock_data(list(self.held_stocks.keys()))
        li = []
        for code, user_stock in self.held_stocks.items():
            if code not in data:
                li.append(f'未知({code})')
            else:
                profit_pct = user_stock.calc_profit_percent(data[code].current_price)
                profit_sign = "+" if profit_pct >= 0 else "-"
                if user_stock.cnt != 0:
                    li.append(f"{data[code].name}({code})x{user_stock.cnt} 值{data[code].current_price * user_stock.cnt:.2f}{VOUCHER_UNIT}{VOUCHER_NAME} {profit_sign}{abs(profit_pct):.1f}%")
        
        return li
    


#买入 卖出 股票(当前持股)


@route('stock')
@enable_backup
class Stock(Plugin, AchvCustomizer):
    gls: GroupLocalStorage[StockMan] = GroupLocalStorage[StockMan]()

    voucher: Inject['Voucher']
    throttle: Inject['Throttle']
    achv: Inject['Achv']
    
    @delegate()
    async def is_achv_deletable(self, e: AchvEnum, source_op: SourceOp):
        if e is StockAchv.MOUTAI:
            ts = await self.achv.get_achv_obtained_ts(e)
            span = datetime.now().replace(tzinfo=None) - datetime.fromtimestamp(ts).replace(tzinfo=None)
            if span.days >= 100:
                return True
            else:
                await source_op.send([f'{100 - span.days}天后才可撤销{e.aka}'])
        return False
    
    @top_instr('股票')
    async def test(self, man: Optional[StockMan]):
        if man is None:
            return ['找不到股票交易记录']
            ...

        li = await man.summary()
        if len(li) == 0:
            return ['还没有持仓任何股票']

        return '\n'.join(li)
    
    @top_instr('卖出', InstrAttr.FORCE_BACKUP)
    @throttle_config(name='股票卖出', max_cooldown_duration=3*24*60*60)
    async def sell(self, q: str, cnt: Optional[int], man: StockMan):
        passed = await self.throttle.do_associated()
        if not passed:
            return

        if cnt is None:
            cnt = 1

        if cnt <= 0:
            return [f'卖出数量错误']

        info = await StockApi.search(q)
        if info is None:
            return [f'找不到名叫"{q}"的股票']
        
        data = await StockApi.get_stock_data([info.code])
        if info.code not in data:
            return [f'无法获取股票"{q}"的数据']

        if not man.has(info.code):
            return [f'尚未买入{info.name}({info.code})']

        if not man.is_satisfied(info.code, cnt):
            return [f'已买入的{info.name}({info.code})数量不足, 无法完成交易']
        
        if info.code == 'sh600519' and await self.achv.has(StockAchv.MOUTAI): # 茅台
            return [f'由于成就{StockAchv.MOUTAI.aka}尚在, 无法卖出茅台股']

        man.adjust(info.code, -cnt, data[info.code].current_price)

        record: 'VoucherRecord' = await self.voucher.adjust(
            cnt=data[info.code].current_price * cnt, 
            extra=VoucherRecordExtraStock(
                code=info.code,
                cnt=-cnt,
                price=data[info.code].current_price
            )
        )

        await self.throttle.reset()

        return [f'成功卖出了{cnt}股{info.name}({info.code}), 获得收益{record.count}{VOUCHER_UNIT}{VOUCHER_NAME}']


    @top_instr('买入', InstrAttr.FORCE_BACKUP)
    async def buy(self, q: str, cnt: Optional[int], man: StockMan):
        if cnt is None:
            cnt = 1

        if cnt <= 0:
            return [f'买入数量错误']

        info = await StockApi.search(q)
        if info is None:
            return [f'找不到名叫"{q}"的股票']
        
        data = await StockApi.get_stock_data([info.code])
        if info.code not in data:
            return [f'无法获取股票"{q}"的数据']
        
        total_price = voucher_round_half_up(data[info.code].current_price * cnt)
        await self.voucher.check_satisfied(cnt=total_price)

        man.chat_state = ChatStateBuyConfirming(code=info.code, cnt=cnt, price=data[info.code].current_price)
        return [f'确定要花费{total_price}{VOUCHER_UNIT}{VOUCHER_NAME}买入{cnt}股{info.name}({info.code})吗? 回复"y"确认, 回复其他取消']
        # data[info.code].current_price * cnt

    @any_instr()
    async def in_flow_buy_confirm(self, aka: str, man: StockMan):
        if isinstance(man.chat_state, ChatStateBuyConfirming):
            try:
                if 'y' in aka.lower():
                    total_price = man.chat_state.price * man.chat_state.cnt
                    await self.voucher.adjust(cnt=-total_price, extra=VoucherRecordExtraStock(
                        code=man.chat_state.code,
                        cnt=man.chat_state.cnt,
                        price=man.chat_state.price
                    ))
                    man.adjust(man.chat_state.code, man.chat_state.cnt, man.chat_state.price)

                    if man.chat_state.code == 'sh600519': # 茅台
                        await self.achv.submit(StockAchv.MOUTAI)

                    return ['买入成功']
            finally: 
                self.backup_man.set_dirty()
                man.chat_state = ChatStateIdle()
            ...
        ...

    # @top_instr('股票测试')
    # async def test(self):
    #     ctx = MiniRacer()
    #     ctx.eval('v_usTSM="200~台积电~TSM.N~158.75~141.37~140.20~45612516~0~0~158.58~5~0~0~0~0~0~0~0~0~159.50~1~0~0~0~0~0~0~0~0~~2025-04-09 16:06:46~17.38~12.29~160.62~137.90~USD~45612516~6843758344~0.88~23.00~~23.00~1:5~16.07~8207.03352~8233.64273~Taiwan Semiconductor Manufacturing~6.90~225.72~124.41~4~6.37~1.29~8233.64273~-19.37~-6.83~GP~30.00~19.01~-8.50~-10.13~-20.92~5186546600~5169784895~1.51~23.00~2.05~150.04~~~";')

    #     ...
    # ...