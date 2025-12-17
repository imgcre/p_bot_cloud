from typing import Final
from plugin import Plugin, enable_backup, route, top_instr

@route('银行')
@enable_backup
class Bank(Plugin):
    ANNUAL_INTEREST_RATE: Final = 0.15

    @top_instr('存款')
    async def store_balance_cmd(self):
        ...

    @top_instr('取款')
    async def store_balance_cmd(self):
        ...

    

    ...
