
import asyncio
import time
from plugin import Context, InstrAttr, Plugin, autorun, delegate, enable_backup, route
from utilities import RecallItem
import traceback

@route('recall_scheduler')
@enable_backup
class RecallScheduler(Plugin):
    items: set[RecallItem] = set[RecallItem]()
    
    @delegate(InstrAttr.FORCE_BACKUP)
    async def add_item(self, *, it: RecallItem):
        self.items.add(it)
        return it

    @autorun
    async def recall_task(self, ctx: Context):
        await asyncio.sleep(5)
        while True:
            await asyncio.sleep(1)
            now = time.time()
            with ctx:
                will_recall_items = [it for it in self.items if it.recall_ts is not None and it.recall_ts < now]
                if len(will_recall_items) > 0:
                    print(f'{will_recall_items=}')
                    self.items.difference_update(will_recall_items)
                    self.backup_man.set_dirty()
                    for it in will_recall_items:
                        try: await self.bot.recall(it.msg_id, it.target_id)
                        except: ... #traceback.print_exc()