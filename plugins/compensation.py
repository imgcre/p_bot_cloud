from decimal import Decimal
import json
from plugin import Inject, Plugin, enable_backup, route, top_instr
from dataclasses import dataclass

from utilities import UserSpec, VoucherRecordExtraLiveGiftCompensation, get_logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugins.live import Live
    from plugins.voucher import Voucher, VoucherRecord

logger = get_logger()

@dataclass
class CompensationInfoMan():
    is_claimed: bool = False
    ...

@route('compensate')
@enable_backup
class Compensation(Plugin):
    user_compensation_info: UserSpec[CompensationInfoMan] = UserSpec[CompensationInfoMan]()

    live: Inject['Live']
    voucher: Inject['Voucher']

    @top_instr('补领兑奖券')
    async def claim_cmd(self, man: CompensationInfoMan):
        if man.is_claimed:
            return '已经补领过兑奖券了'
        
        name = await self.live.get_associated_name()
        if name is None:
            return '请先【#绑定账号】'

        with open(self.path.data.of_file('data.json'), encoding='utf-8') as f:
            data_o: dict[str, int] = json.load(f)

        if name not in data_o:
            return '未查找到投喂记录'

        record: 'VoucherRecord' = await self.voucher.adjust(
            cnt=Decimal(data_o[name]) / 1000 / 10,
            extra=VoucherRecordExtraLiveGiftCompensation()
        )

        man.is_claimed = True
        self.backup_man.set_dirty()

        return f'补领成功, 获得{record.count}张兑奖券'
        

        
