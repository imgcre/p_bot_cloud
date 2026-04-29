from typing import Optional
import typing
from plugin import Plugin, route, top_instr, Inject
from utilities import AchvEnum, AchvInfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugins.achv import Achv

@route('帮助系统')
class Help(Plugin):
    achv: Inject['Achv']
    
    @top_instr('notfound')
    async def notfound(self, cmd_name: str):
        achv: AchvEnum = await self.achv.dynamic_aka_to_achv(cmd_name)
        if achv is not None:
            val = typing.cast(AchvInfo, achv.value)

            tx = [f'{val.aka}: {val.condition}']

            if val.aka != cmd_name:
                tx.append(f'*{cmd_name}是{val.aka}在当前状态下的动态名称')

            return '\n'.join(tx)

        return f'指令"{cmd_name}"不存在'

    @top_instr('帮助')
    async def help(self, sub_cmd: Optional[str]):

        tx = [
            '好的，以下是本群的常见概念：\n',
            '猫德：违规计数，猫德 ≥ 0 是健康状态。使用指令【#猫德】可查询猫德\n',
            '猫条：猫咪积分，使用指令【#猫条】来查询猫条数量及其获取方式。猫条可用来兑换物料（使用指令【#所有物料】可查询当前可兑换的物料）、兑换额外的点歌次数、参与猜拳和股票等游戏。'
        ]

        return tx


        from plugins.admin import AdminAchv
        admin_info: AchvInfo = AdminAchv.ADMIN.value
        
        if sub_cmd is None:
            return [
                '\n'.join([
                    '===帮助页面施工中===',
                    '【子命令】',
                    f'管理: 管理员介绍及指令列表',
                ])
            ]
        
        if sub_cmd == '管理':
            return [
                '\n\n'.join([
                    f'通过【#佩戴 {admin_info.aka}】佩戴管理{admin_info.opts.display}称号后, 即可解锁管理系列指令',
                    f'#撤回: 引用一条群友的消息, 然后回复"#撤回"即可撤回指定消息',
                    f'#公告: 引用一条群友的消息, 然后回复"#公告", 将会导致bot发布一则内容与被引用消息一致的公告',
                    f'#全体: @全体成员',
                    f'代理执行: 引用一条群友的消息, 如果回复的消息中包含被中括号指定的文本, 则将代指定群友执行中括号中的指令。'
                    '如回复的消息内容是"【#来只灯泡】", 则视作该群友自助领取了一颗灯泡'
                ])
            ]
        ...
    
    ...