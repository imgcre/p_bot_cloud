from email.headerregistry import Group
from typing import Optional, Type, TypeVar, Union, overload
import config
from mirai import GroupMessage, MessageChain, At
from plugin import Plugin, delegate, top_instr, route, enable_backup, Inject
from mirai.models.message import Forward, ForwardMessageNode, MessageComponent
from mirai.models.entities import Friend, GroupMember
from dacite import from_dict
from dacite.core import _build_value
import aiohttp
from dataclasses import dataclass

from typing import TYPE_CHECKING

from utilities import User
from nap_cat_types import *

T = TypeVar('T')

@route('NapCat')
@enable_backup
class NapCat(Plugin):

    async def call(self, endpoint: str, data: dict, ret_type: Type[T] = None) -> T:
        async with aiohttp.ClientSession() as session:
            headers = {
               'Authorization': 'Bearer sFUXXyiv07HzX'
            }
            async with session.post(f'http://127.0.0.1:3000{endpoint}', json=data, headers=headers) as resp:
                if ret_type is not None:
                    return _build_value(data_class=ret_type, data=(await resp.json())['data'])

    @delegate()
    async def get_stranger_info(self, user: User) -> GetStrangerInfoResp:
        return await self.call('/get_stranger_info', {
            "user_id": user.id
        }, GetStrangerInfoResp)
    
    async def get_group_member_list(self, group_id: int) -> list[GetGroupMemberListRespItem]:
        return await self.call('/get_group_member_list', {
            "group_id": group_id
        }, list[GetGroupMemberListRespItem])

    
    @overload
    async def get_group_member_info(self) -> GetGroupMemberInfoResp: ...

    @delegate()
    async def get_group_member_info(self, member: GroupMember) -> GetGroupMemberInfoResp:
        return await self.call('/get_group_member_info', {
            "group_id": member.group.id,
            "user_id": member.id,
            "no_cache": True
        }, GetGroupMemberInfoResp)
    
    @delegate()
    async def send_poke(self, member: GroupMember):
        return await self.call('/send_poke', {
            "group_id": member.group.id,
            "user_id": member.id,
        })
    
    async def set_msg_emoji_like(self, message_id: Union[str, int], emoji_id: int):
        return await self.call('/set_msg_emoji_like', {
            "message_id": message_id,
            "emoji_id": emoji_id,
            "set": True,
        })
    
    @top_instr('贴')
    async def like_cmd(self, event: GroupMessage, emoji_id: int):
        await self.set_msg_emoji_like(event.message_chain.message_id, emoji_id)

    @top_instr('贴自己')
    async def like_self_cmd(self, event: GroupMessage, emoji_id: int):
        res = await self.bot.send_group_message(event.group.id, ['好的'])
        await self.set_msg_emoji_like(res.message_id, emoji_id)
    
    @delegate()
    async def send_msg(self, member: GroupMember, *, text: str):
        return await self.call('/send_msg', {
            "message_type": "private",
            "group_id": member.group.id,
            "user_id": member.id,
            "message": [
                {
                    "type": "text",
                    "data": {
                        "text": text
                    }
                }
            ]
        })
