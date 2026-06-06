import asyncio
import os
import subprocess
from typing import TYPE_CHECKING
from event_types import LiveStartedEvent, LiveStoppedEvent
from plugin import Context, Inject, Plugin, any_instr, autorun, delegate, instr, InstrAttr, route, PathArg, top_instr
import gc
import time
from collections import defaultdict

from utilities import handler

if TYPE_CHECKING:
    from plugins.events import Events
    from plugins.known_groups import KnownGroups

class IncrementalObjectTracker:
    def __init__(self):
        self.baseline = self._capture_baseline()
        self.last_check = time.time()
    
    def _capture_baseline(self):
        """捕获基线，只记录类型和数量"""
        baseline = defaultdict(int)
        for obj in gc.get_objects():
            obj_type = type(obj).__name__
            baseline[obj_type] += 1
        return dict(baseline)
    
    def check_growth(self):
        """检查对象增长情况"""
        current = defaultdict(int)
        for obj in gc.get_objects():
            obj_type = type(obj).__name__
            current[obj_type] += 1
        
        # 计算增长
        growth = {}
        for obj_type in set(self.baseline) | set(current):
            diff = current.get(obj_type, 0) - self.baseline.get(obj_type, 0)
            if diff > 0:  # 只关心增长
                growth[obj_type] = diff
        
        # 更新基线（可选）
        # self.baseline = dict(current)
        
        return growth
    
    def find_leaking_types(self, threshold=100):
        """找出显著增长的对象类型"""
        growth = self.check_growth()
        leaking = {
            obj_type: count 
            for obj_type, count in growth.items() 
            if count > threshold
        }
        return leaking

@route('man')
class Man(Plugin):
    has_said_goodbye: bool = False

    events: Inject['Events']
    known_groups: Inject['KnownGroups']
    
    def __init__(self):
        self.tracker = None

    @top_instr('记录')
    async def start_rec_cmd(self):
        self.tracker = IncrementalObjectTracker()
        return 'ok'
    
    @top_instr('报告')
    async def report_rec_cmd(self):
        if self.tracker is None:
            return '未开始'
        leaking_types = self.tracker.find_leaking_types(threshold=50)
        print("可能泄漏的对象类型:")
        for obj_type, growth in leaking_types.items():
            print(f"  {obj_type}: 增长了 {growth} 个")
        return 'ok'
    
    @top_instr('内存监控')
    async def monitor_mem_cmd(self):
        try:
            import objgraph
        except ImportError:
            return '缺少 objgraph 依赖'
        objgraph.show_backrefs(
            objgraph.by_type('deque')[:5], 
            max_depth=10,
            filename='/root/projects/dat.dot'
        )

    @any_instr()
    async def update_chat_lock(self):
        subprocess.Popen('touch /root/projects/p_bot_man/chat.lock', shell=True)

    @handler
    async def on_live_started(self, event: LiveStartedEvent):
        subprocess.Popen('touch /root/projects/p_bot_man/live.lock', shell=True)

    @handler
    async def on_live_stopped(self, event: LiveStoppedEvent):
        subprocess.Popen('rm -f /root/projects/p_bot_man/live.lock', shell=True)

    @delegate(InstrAttr.FORCE_BACKUP)
    async def bye(self):
        self.has_said_goodbye = True
        for group_id in self.known_groups:
            await self.bot.send_group_message(group_id, [
                '睡觉啦💤'
            ])

    @delegate()
    async def hello(self):
        if self.has_said_goodbye:
            self.backup_man.set_dirty()
            self.has_said_goodbye = False
            for group_id in self.known_groups:
                await self.bot.send_group_message(group_id, [
                    '睡醒啦~'
                ])

    @autorun
    async def auto_hello(self, ctx: Context):
        await asyncio.sleep(1)
        with ctx:
            await self.hello()
    
    # @instr('list', InstrAttr.NO_ALERT_CALLER)
    # @admin
    # async def list(self):
    #     ll = []
    #     for k in self.engine.plugins.keys():
    #         p = self.engine.plugins[k]
    #         enabled_str = '已启用' if not p.disabled else '已禁用'
    #         ll.append(f'{k} {enabled_str}')
    #     return '\n'.join(ll)

    # @instr('(?P<state>enable|disable)', InstrAttr.NO_ALERT_CALLER)
    # @admin
    # async def disable(self, plugin_name: str, state: PathArg[str]):
    #     print(f'next state -> {state}')
    #     def change_state(p: Plugin):
    #         if state == 'enable':
    #             p.enable()
    #         else:
    #             p.disable()

    #     if plugin_name == 'all':
    #         for p in self.engine.plugins.values():
    #             if p is not self:
    #                 change_state(p)
    #     else:
    #         if plugin_name not in self.engine.plugins:
    #             return '指定插件不存在'
    #         change_state(self.engine.plugins[plugin_name])
    #     return f'{plugin_name} -> {state}'
    ...
