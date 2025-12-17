from dataclasses import dataclass

@dataclass
class GetStrangerInfoResp():
    qqLevel: int

@dataclass
class GetGroupMemberInfoResp():
    nickname: str # 账号名
    card: str # 群昵称, 可能为空字符串
    age: int
    level: str
    qq_level: int
    title: str # 可能为空字符串
    join_time: int # 秒
    last_sent_time: int
    ...