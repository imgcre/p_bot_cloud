from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Type

import websockets
from dacite import Config
from dacite.core import _build_value
from mirai import At, AtAll, Face, FriendMessage, GroupMessage, Image, MessageChain, Plain, TempMessage, Voice
from mirai.asgi import ASGI
from mirai.models.api import RespOperate
from mirai.models.entities import Friend, Group, GroupConfigModel, GroupMember, Permission, Subject
from mirai.models.events import (
    Event,
    MemberCardChangeEvent,
    GroupRecallEvent,
    MemberJoinRequestEvent,
    MemberJoinEvent,
    MemberUnmuteEvent,
    MessageEvent,
    NudgeEvent,
    StrangerMessage,
)
from mirai.models.message import App, File, Forward, ForwardMessageNode, MessageComponent, MusicShare, Quote, Source

import mirai_compat  # noqa: F401
from mirai.models.message import MarketFace, ShortVideo
from utilities import get_logger


Handler = Callable[[Any], Awaitable[None]]
logger = get_logger()


@dataclass
class ActionResponse:
    status: str = "ok"
    retcode: int = 0
    data: Any = None
    message: str = ""

    def __getattr__(self, item: str) -> Any:
        return getattr(self.data, item)

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, item):
        return self.data[item]


class MessageResponse(ActionResponse):
    @property
    def message_id(self) -> int:
        if isinstance(self.data, dict):
            return int(self.data.get("message_id", -1))
        return int(getattr(self.data, "message_id", -1))


class ListResponse(ActionResponse):
    def __iter__(self):
        return iter(self.data or [])


class MessageFromIdResponse(ActionResponse):
    @property
    def message_chain(self) -> MessageChain:
        return self.data.message_chain


class ResourceAccessor:
    def __init__(
        self,
        getter: Callable[..., Awaitable[Any]],
        setter: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._getter = getter
        self._setter = setter

    async def get(self, *args):
        return await self._getter(*args)

    async def __call__(self, *args):
        return await self.get(*args)

    async def set(self, *args):
        if self._setter is None:
            raise NotImplementedError("set is not supported")
        return await self._setter(*args)


class NapCatBot:
    def __init__(
        self,
        qq: int,
        *,
        ws_url: str = "ws://127.0.0.1:3001",
        access_token: Optional[str] = None,
        reconnect_interval: float = 5.0,
        api_timeout: float = 30.0,
    ) -> None:
        self.qq = qq
        self.ws_url = ws_url
        self.access_token = access_token
        self.reconnect_interval = reconnect_interval
        self.api_timeout = api_timeout
        self._handlers: list[tuple[Type[Any], Handler]] = []
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self._ws = None
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected_events: dict[asyncio.AbstractEventLoop, asyncio.Event] = {}
        self._stopping = False
        self._background_tasks: list[asyncio.Task] = []
        self._event_tasks: set[asyncio.Task] = set()
        self._group_cache: dict[int, Group] = {}
        self._member_cache: dict[tuple[int, int], GroupMember] = {}
        self.asgi = ASGI()

        self.group_list = ResourceAccessor(self._get_group_list)
        self.member_list = ResourceAccessor(self._get_member_list)
        self.friend_list = ResourceAccessor(self._get_friend_list)

    def on(self, event_type: Type[Any]) -> Callable[[Handler], Handler]:
        def decorator(func: Handler) -> Handler:
            self._handlers.append((event_type, func))
            return func

        return decorator

    def add_background_task(self, func=None):
        return self.asgi.add_background_task(func)

    def _connected_event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        event = self._connected_events.get(loop)
        if event is None:
            event = asyncio.Event()
            self._connected_events[loop] = event
        if self._ws is None:
            event.clear()
        else:
            event.set()
        return event

    def _publish_connected(self, connected: bool) -> None:
        current_loop = asyncio.get_running_loop()
        if connected:
            self._ws_loop = current_loop
        elif self._ws_loop is current_loop:
            self._ws_loop = None

        for loop, event in list(self._connected_events.items()):
            if loop.is_closed():
                self._connected_events.pop(loop, None)
                continue
            callback = event.set if connected else event.clear
            if loop is current_loop:
                callback()
            else:
                loop.call_soon_threadsafe(callback)

    async def startup(self) -> None:
        self._stopping = False

    async def shutdown(self) -> None:
        logger.info("napcat bot shutdown requested")
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()
        for _, future in list(self._pending.values()):
            if not future.done():
                future.cancel()

    async def background(self) -> None:
        while not self._stopping:
            connected = False
            try:
                headers = {}
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                logger.info(
                    "napcat ws connecting url=%s access_token=%s",
                    self.ws_url,
                    bool(self.access_token),
                )
                async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
                    self._ws = ws
                    connected = True
                    self._publish_connected(True)
                    logger.info("napcat ws connected url=%s", self.ws_url)
                    async for raw in ws:
                        await self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ws = None
                self._publish_connected(False)
                if not self._stopping:
                    logger.warning(
                        "napcat ws %s url=%s error=%r; reconnect in %.1fs",
                        "disconnected" if connected else "connect failed",
                        self.ws_url,
                        exc,
                        self.reconnect_interval,
                        exc_info=True,
                    )
                    await asyncio.sleep(self.reconnect_interval)
            finally:
                if connected:
                    logger.info("napcat ws disconnected url=%s", self.ws_url)
                self._ws = None
                self._publish_connected(False)

    def run(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        asgi_server: str = "auto",
        **kwargs,
    ) -> None:
        self.asgi.add_event_handler("startup", self.startup)
        self.asgi.add_event_handler("startup", self._start_background)
        self.asgi.add_event_handler("shutdown", self.shutdown)
        if asgi_server in ("auto", "uvicorn"):
            try:
                import uvicorn
                uvicorn.run(self.asgi, host=host, port=port, **kwargs)
                return
            except ImportError:
                if asgi_server == "uvicorn":
                    raise
        if asgi_server in ("auto", "hypercorn"):
            try:
                from hypercorn.asyncio import serve
                from hypercorn.config import Config as HypercornConfig

                hypercorn_config = HypercornConfig()
                hypercorn_config.bind = [f"{host}:{port}"]
                for key, value in kwargs.items():
                    setattr(hypercorn_config, key, value)
                asyncio.run(serve(self.asgi, hypercorn_config))
                return
            except ImportError:
                if asgi_server == "hypercorn":
                    raise
        asyncio.run(self._run_without_asgi())

    async def _run_without_asgi(self) -> None:
        await self.startup()
        await self.background()

    async def _start_background(self) -> None:
        task = asyncio.create_task(self.background())
        self._background_tasks.append(task)
        logger.info("napcat ws background task started")

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("napcat ws received invalid json raw=%s", self._preview(raw), exc_info=True)
            raise

        logger.debug("napcat ws frame received %s raw=%s", self._payload_summary(payload), self._preview(raw))
        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            action, future = self._pending.pop(echo)
            logger.debug(
                "napcat api response matched action=%s echo=%s status=%s retcode=%s",
                action,
                echo,
                payload.get("status"),
                payload.get("retcode"),
            )
            if not future.done():
                future.set_result(payload)
            return
        if echo is not None:
            logger.warning("napcat api response has unknown echo=%s %s", echo, self._payload_summary(payload))
            return

        event = await self._event_from_onebot(payload)
        if event is None:
            logger.debug("napcat event ignored %s", self._payload_summary(payload))
            return
        logger.info("napcat event received %s", self._event_summary(event))
        self._schedule_event(event)

    def _schedule_event(self, event: Any) -> None:
        task = asyncio.create_task(self._emit(event))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)
        logger.debug("napcat event scheduled %s pending_event_tasks=%d", self._event_summary(event), len(self._event_tasks))

    async def _emit(self, event: Any) -> None:
        try:
            await self._hydrate_event_quotes(event)
        except Exception:
            logger.warning("napcat event quote hydration failed %s", self._event_summary(event), exc_info=True)
        for event_type, handler in list(self._handlers):
            if isinstance(event, event_type):
                await handler(event)

    async def call_action(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        *,
        response_cls: Type[ActionResponse] = ActionResponse,
    ) -> ActionResponse:
        while True:
            await self._connected_event().wait()
            ws_loop = self._ws_loop
            current_loop = asyncio.get_running_loop()
            if self._ws is None or ws_loop is None:
                self._connected_event().clear()
                continue
            if ws_loop is current_loop:
                return await self._call_action_connected(action, params, response_cls=response_cls)
            if ws_loop.is_closed() or not ws_loop.is_running():
                self._connected_event().clear()
                continue
            future = asyncio.run_coroutine_threadsafe(
                self._call_action_connected(action, params, response_cls=response_cls),
                ws_loop,
            )
            return await asyncio.wrap_future(future)

    async def _call_action_connected(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        *,
        response_cls: Type[ActionResponse] = ActionResponse,
    ) -> ActionResponse:
        if self._ws is None:
            raise RuntimeError("NapCat websocket is not connected")
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[echo] = (action, future)
        req = {
            "action": action,
            "params": params or {},
            "echo": echo,
        }
        logger.debug(
            "napcat api request action=%s echo=%s params=%s",
            action,
            echo,
            self._preview(params or {}),
        )
        await self._ws.send(json.dumps(req, ensure_ascii=False))
        try:
            payload = await asyncio.wait_for(future, timeout=self.api_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            logger.warning(
                "napcat api timeout action=%s echo=%s timeout=%.1fs pending=%d",
                action,
                echo,
                self.api_timeout,
                len(self._pending),
            )
            raise
        except Exception:
            self._pending.pop(echo, None)
            logger.warning("napcat api failed action=%s echo=%s", action, echo, exc_info=True)
            raise
        message = payload.get("message") or payload.get("wording") or ""
        resp = response_cls(
            status=payload.get("status", ""),
            retcode=int(payload.get("retcode", -1)),
            data=payload.get("data"),
            message=message,
        )
        if resp.status not in ("ok", "async") or resp.retcode not in (0, 1):
            raise RuntimeError(f"NapCat action failed: {action}, {resp.status}, {resp.retcode}, {resp.message}")
        logger.debug("napcat api response ok action=%s echo=%s retcode=%s", action, echo, resp.retcode)
        return resp

    def _preview(self, value: Any, limit: int = 700) -> str:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        elif isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except TypeError:
                text = str(value)
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        if len(text) > limit:
            return f"{text[:limit]}...({len(text)} chars)"
        return text

    def _payload_summary(self, payload: dict[str, Any]) -> str:
        parts = []
        for key in ("post_type", "message_type", "notice_type", "request_type", "sub_type", "action", "status", "retcode"):
            value = payload.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        if payload.get("echo") is not None:
            parts.append(f"echo={payload.get('echo')}")
        if payload.get("group_id") is not None:
            parts.append(f"group_id={payload.get('group_id')}")
        if payload.get("user_id") is not None:
            parts.append(f"user_id={payload.get('user_id')}")
        if payload.get("message_id") is not None:
            parts.append(f"message_id={payload.get('message_id')}")
        return " ".join(parts) or "payload=unknown"

    def _event_summary(self, event: Any) -> str:
        try:
            if isinstance(event, GroupMessage):
                return (
                    f"GroupMessage group_id={getattr(getattr(event, 'group', None), 'id', None)} "
                    f"sender_id={getattr(getattr(event, 'sender', None), 'id', None)} "
                    f"message_id={getattr(getattr(event, 'message_chain', None), 'message_id', None)}"
                )
            if isinstance(event, TempMessage):
                return (
                    f"TempMessage group_id={getattr(getattr(event, 'group', None), 'id', None)} "
                    f"sender_id={getattr(getattr(event, 'sender', None), 'id', None)} "
                    f"message_id={getattr(getattr(event, 'message_chain', None), 'message_id', None)}"
                )
            if isinstance(event, FriendMessage):
                return (
                    f"FriendMessage sender_id={getattr(getattr(event, 'sender', None), 'id', None)} "
                    f"message_id={getattr(getattr(event, 'message_chain', None), 'message_id', None)}"
                )
            if isinstance(event, MemberJoinRequestEvent):
                return (
                    "MemberJoinRequestEvent "
                    f"group_id={getattr(event, 'group_id', getattr(event, 'groupId', None))} "
                    f"from_id={getattr(event, 'from_id', getattr(event, 'fromId', None))}"
                )
            if isinstance(event, NudgeEvent):
                subject = getattr(event, "subject", None)
                return (
                    f"NudgeEvent subject={getattr(subject, 'kind', None)}:{getattr(subject, 'id', None)} "
                    f"from_id={getattr(event, 'from_id', getattr(event, 'fromId', None))} "
                    f"target={getattr(event, 'target', None)}"
                )
            if isinstance(event, GroupRecallEvent):
                return (
                    f"GroupRecallEvent group_id={getattr(getattr(event, 'group', None), 'id', None)} "
                    f"message_id={getattr(event, 'message_id', getattr(event, 'messageId', None))} "
                    f"author_id={getattr(event, 'author_id', getattr(event, 'authorId', None))}"
                )
            if isinstance(event, (MemberJoinEvent, MemberUnmuteEvent, MemberCardChangeEvent)):
                member = getattr(event, "member", None)
                group = getattr(member, "group", None)
                return f"{type(event).__name__} group_id={getattr(group, 'id', None)} member_id={getattr(member, 'id', None)}"
        except Exception:
            logger.debug("napcat event summary failed type=%s", type(event).__name__, exc_info=True)
        return type(event).__name__

    async def call_action_data(self, action: str, params: Optional[dict[str, Any]] = None) -> Any:
        return (await self.call_action(action, params)).data

    async def call_action_model(self, action: str, params: dict[str, Any], ret_type: Type[Any]) -> Any:
        data = await self.call_action_data(action, params)
        return _build_value(type_=ret_type, data=data, config=Config())

    async def send_group_message(
        self,
        target: Optional[int] = None,
        message_chain=None,
        quote: Optional[int] = None,
        *args,
        **kwargs,
    ) -> MessageResponse:
        group_id = target
        if group_id is None and args:
            group_id = args[0]
            args = args[1:]
        if message_chain is None:
            message_chain = kwargs.get("message")
        if message_chain is None and args:
            message_chain = args[0]
        forward = self._extract_forward(message_chain)
        if forward is not None:
            return await self._send_group_forward_message(group_id, forward)
        params = {
            "group_id": group_id,
            "message": self._message_to_onebot(message_chain, quote=quote),
        }
        return await self.call_action("send_group_msg", params, response_cls=MessageResponse)

    async def send_friend_message(
        self,
        target: Optional[int] = None,
        message_chain=None,
        quote: Optional[int] = None,
        *args,
        **kwargs,
    ) -> MessageResponse:
        user_id = target
        if user_id is None and args:
            user_id = args[0]
            args = args[1:]
        if message_chain is None:
            message_chain = kwargs.get("message")
        if message_chain is None and args:
            message_chain = args[0]
        forward = self._extract_forward(message_chain)
        if forward is not None:
            return await self._send_private_forward_message(user_id, forward)
        params = {
            "user_id": user_id,
            "message": self._message_to_onebot(message_chain, quote=quote),
        }
        return await self.call_action("send_private_msg", params, response_cls=MessageResponse)

    async def send_temp_message(
        self,
        qq: int,
        group: int,
        message_chain=None,
        quote: Optional[int] = None,
        *args,
        **kwargs,
    ) -> MessageResponse:
        if message_chain is None:
            message_chain = kwargs.get("message")
        if message_chain is None and args:
            message_chain = args[0]
        forward = self._extract_forward(message_chain)
        if forward is not None:
            return await self._send_private_forward_message(qq, forward)
        params = {
            "message_type": "private",
            "user_id": qq,
            "group_id": group,
            "message": self._message_to_onebot(message_chain, quote=quote),
        }
        return await self.call_action("send_msg", params, response_cls=MessageResponse)

    async def send(self, target, message, quote: bool = False) -> int:
        quoting = None
        if isinstance(target, TempMessage):
            quoting = target.message_chain.message_id if quote else None
            return (await self.send_temp_message(target.sender.id, target.group.id, message, quote=quoting)).message_id
        if isinstance(target, MessageEvent):
            quoting = target.message_chain.message_id if quote else None
            target = target.sender
        if isinstance(target, Friend):
            return (await self.send_friend_message(target.id, message, quote=quoting)).message_id
        if isinstance(target, Group):
            return (await self.send_group_message(target.id, message, quote=quoting)).message_id
        if isinstance(target, GroupMember):
            return (await self.send_group_message(target.group.id, message, quote=quoting)).message_id
        raise ValueError(f"{target} is not a valid message target")

    async def recall(self, message_id: int, group: Optional[int] = None):
        return await self.call_action("delete_msg", {"message_id": message_id})

    async def mute(self, group: int, member: int, time_s: int):
        group_id = int(group)
        user_id = int(member)
        duration = max(0, int(time_s))
        resp = await self.call_action("set_group_ban", {
            "group_id": group_id,
            "user_id": user_id,
            "duration": duration,
        })
        cached = self._member_cache.get((group_id, user_id))
        if cached is not None:
            cached.mute_time_remaining = duration
        return resp

    async def unmute(self, group: int, member: int):
        return await self.mute(group, member, 0)

    async def kick(self, group: int, member: int, msg: str = "", reject_add_request: bool = False):
        group_id = int(group)
        user_id = int(member)
        resp = await self.call_action("set_group_kick", {
            "group_id": group_id,
            "user_id": user_id,
            "reject_add_request": bool(reject_add_request),
        })
        self._member_cache.pop((group_id, user_id), None)
        return resp

    async def member_admin(self, group: int, member: int, assign: bool):
        group_id = int(group)
        user_id = int(member)
        enabled = bool(assign)
        resp = await self.call_action("set_group_admin", {
            "group_id": group_id,
            "user_id": user_id,
            "enable": enabled,
        })
        cached = self._member_cache.get((group_id, user_id))
        if cached is not None:
            cached.permission = Permission.Administrator if enabled else Permission.Member
        return resp

    async def send_nudge(self, target: int, subject: int, kind: str):
        if kind == "Group":
            return await self.call_action("send_poke", {"group_id": subject, "user_id": target})
        return await self.call_action("send_poke", {"user_id": target})

    async def resp_member_join_request_event(
        self,
        event_id: int,
        from_id: int,
        group_id: int,
        operate: RespOperate,
        message: str = "",
    ):
        return await self.call_action("set_group_add_request", {
            "flag": str(event_id),
            "approve": self._is_request_approved(operate),
            "reason": message,
        })

    async def get_group(self, id_: int) -> Optional[Group]:
        group = self._group_cache.get(int(id_))
        if group is not None:
            return group
        return await self._fetch_group(id_, no_cache=False)

    async def _fetch_group(self, id_: int, *, no_cache: bool) -> Optional[Group]:
        data = await self.call_action_data("get_group_info", {"group_id": int(id_), "no_cache": no_cache})
        group = self._group_from_onebot(data)
        self._group_cache[group.id] = group
        return group

    async def get_group_member(self, group, id_: int) -> Optional[GroupMember]:
        group_id = group.id if isinstance(group, Group) else int(group)
        cached = self._member_cache.get((group_id, int(id_)))
        if cached is not None:
            return cached
        try:
            data = await self.call_action_data("get_group_member_info", {
                "group_id": group_id,
                "user_id": id_,
                "no_cache": False,
            })
        except Exception:
            return None
        member = await self._member_from_onebot(data, group_id=group_id)
        self._member_cache[(group_id, member.id)] = member
        return member

    async def _get_group_list(self) -> list[Group]:
        data = await self.call_action_data("get_group_list", {"no_cache": False})
        groups = [self._group_from_onebot(item) for item in data or []]
        self._group_cache.update({g.id: g for g in groups})
        return groups

    async def _get_friend_list(self) -> list[Friend]:
        data = await self.call_action_data("get_friend_list", {"no_cache": False})
        return [Friend(id=int(item.get("user_id")), nickname=item.get("nickname"), remark=item.get("remark")) for item in data or []]

    async def _get_member_list(self, group: int) -> ListResponse:
        data = await self.call_action_data("get_group_member_list", {"group_id": group, "no_cache": False})
        members = [await self._member_from_onebot(item, group_id=group) for item in data or []]
        for member in members:
            self._member_cache[(member.group.id, member.id)] = member
        return ListResponse(data=members)

    def _get_member_info_update_value(self, info: Any, *names: str):
        fields_set = getattr(info, "__fields_set__", None)
        if fields_set is None:
            fields_set = getattr(info, "model_fields_set", None)

        candidate_names = list(dict.fromkeys(names))
        aliases = getattr(info, "__fields__", None)
        if aliases:
            for field_name, field in aliases.items():
                alias = getattr(field, "alias", None)
                if not alias:
                    continue
                if field_name in candidate_names and alias not in candidate_names:
                    candidate_names.append(alias)
                if alias in candidate_names and field_name not in candidate_names:
                    candidate_names.append(field_name)

        if isinstance(info, dict):
            for name in candidate_names:
                if name in info:
                    return info[name]
            return None

        for name in candidate_names:
            if fields_set is not None and name not in fields_set:
                continue
            if hasattr(info, name):
                return getattr(info, name)

        if fields_set is not None:
            return None

        for name in candidate_names:
            if hasattr(info, name):
                return getattr(info, name)
        return None

    def member_info(self):
        bot = self

        class MemberInfoAccessor:
            async def set(self, group: int, member: int, info: Any):
                group_card = bot._get_member_info_update_value(info, "member_name", "memberName", "name", "card")
                special_title = bot._get_member_info_update_value(info, "special_title", "specialTitle")
                changed = False
                if group_card is not None:
                    await bot.call_action("set_group_card", {
                        "group_id": group,
                        "user_id": member,
                        "card": group_card,
                    })
                    cached = bot._member_cache.get((int(group), int(member)))
                    if cached is not None:
                        cached.member_name = group_card
                    changed = True
                if special_title is not None:
                    await bot.call_action("set_group_special_title", {
                        "group_id": group,
                        "user_id": member,
                        "special_title": special_title,
                    })
                    cached = bot._member_cache.get((int(group), int(member)))
                    if cached is not None:
                        cached.special_title = special_title
                    changed = True
                if not changed:
                    logger.debug("member_info.set ignored empty update group_id=%s user_id=%s info=%r", group, member, info)

        return MemberInfoAccessor()

    def group_config(self, group: int):
        bot = self

        class GroupConfigAccessor:
            async def get(self):
                group_obj = await bot._fetch_group(group, no_cache=True)
                return GroupConfigModel(
                    name=group_obj.name if group_obj else str(group),
                    confessTalk=False,
                    allowMemberInvite=False,
                    autoApprove=False,
                    anonymousChat=False,
                )

            async def set(self, conf: GroupConfigModel):
                group_id = int(group)
                name = str(conf.name)
                resp = await bot.call_action("set_group_name", {
                    "group_id": group_id,
                    "group_name": name,
                })
                cached = bot._group_cache.get(group_id)
                if cached is None:
                    bot._group_cache[group_id] = Group(
                        id=group_id,
                        name=name,
                        permission=Permission.Member,
                    )
                else:
                    cached.name = name
                return resp

        return GroupConfigAccessor()

    async def message_from_id(self, message_id: int, group: Optional[int] = None) -> MessageFromIdResponse:
        data = await self.call_action_data("get_msg", {"message_id": message_id})
        event = await self._message_event_from_get_msg(data, group)
        return MessageFromIdResponse(data=event)

    async def file_mkdir(self, parent: str, group: int, name: str):
        return await self.call_action("create_group_file_folder", {
            "group_id": group,
            "name": name,
            "parent_id": parent,
        })

    async def anno_publish(self, group: int, content: str, **kwargs):
        params = {
            "group_id": group,
            "content": content,
        }
        params.update(kwargs)
        return await self.call_action("_send_group_notice", params)

    async def _send_group_forward_message(self, group_id: int, forward: Forward) -> MessageResponse:
        params = {
            "group_id": group_id,
            "messages": [self._forward_node_to_onebot(n) for n in forward.node_list],
        }
        return await self.call_action("send_group_forward_msg", params, response_cls=MessageResponse)

    async def _send_private_forward_message(self, user_id: int, forward: Forward) -> MessageResponse:
        params = {
            "user_id": user_id,
            "messages": [self._forward_node_to_onebot(n) for n in forward.node_list],
        }
        return await self.call_action("send_private_forward_msg", params, response_cls=MessageResponse)

    def _extract_forward(self, message) -> Optional[Forward]:
        if isinstance(message, Forward):
            return message
        if isinstance(message, MessageChain):
            items = list(message)
        elif isinstance(message, (list, tuple)):
            items = list(message)
        else:
            return None
        if len(items) == 1 and isinstance(items[0], Forward):
            return items[0]
        return None

    def _message_to_onebot(self, message, *, quote: Optional[int] = None) -> list[dict[str, Any]]:
        if isinstance(message, MessageChain):
            components = list(message)
        elif isinstance(message, MessageComponent):
            components = [message]
        elif isinstance(message, str):
            components = [Plain(message)]
        else:
            components = [Plain(x) if isinstance(x, str) else x for x in (message or [])]

        segments = []
        if quote is not None:
            segments.append({"type": "reply", "data": {"id": str(quote)}})
        for comp in components:
            if isinstance(comp, Source):
                continue
            segments.extend(self._component_to_onebot(comp))
        return segments

    def _component_to_onebot(self, comp) -> list[dict[str, Any]]:
        if isinstance(comp, str):
            return [{"type": "text", "data": {"text": comp}}]
        if isinstance(comp, Plain):
            return [{"type": "text", "data": {"text": comp.text}}]
        if isinstance(comp, At):
            return [{"type": "at", "data": {"qq": str(comp.target)}}]
        if isinstance(comp, AtAll):
            return [{"type": "at", "data": {"qq": "all"}}]
        if isinstance(comp, Face):
            return [{"type": "face", "data": {"id": str(comp.face_id)}}]
        if isinstance(comp, Image):
            return [{"type": "image", "data": {"file": self._media_file(comp)}}]
        if isinstance(comp, Voice):
            return [{"type": "record", "data": {"file": self._media_file(comp)}}]
        if isinstance(comp, Quote):
            return [{"type": "reply", "data": {"id": str(comp.id)}}]
        if isinstance(comp, App):
            return [{"type": "json", "data": {"data": comp.content}}]
        if isinstance(comp, MusicShare):
            return [{
                "type": "music",
                "data": {
                    "type": str(comp.kind),
                    "url": str(comp.jump_url),
                    "image": str(comp.picture_url),
                    "title": comp.title,
                    "content": comp.summary,
                },
            }]
        if isinstance(comp, Forward):
            return [{"type": "text", "data": {"text": "[聊天记录]"}}]
        return [{"type": "text", "data": {"text": str(comp)}}]

    def _forward_node_to_onebot(self, node: ForwardMessageNode) -> dict[str, Any]:
        return {
            "type": "node",
            "data": {
                "name": node.sender_name or str(node.sender_id or self.qq),
                "uin": str(node.sender_id or self.qq),
                "content": self._message_to_onebot(node.message_chain or []),
            },
        }

    def _media_file(self, comp) -> str:
        if getattr(comp, "base64", None):
            return f"base64://{comp.base64}"
        if getattr(comp, "path", None):
            return str(comp.path)
        if getattr(comp, "url", None):
            return str(comp.url)
        image_id = getattr(comp, "image_id", None) or getattr(comp, "voice_id", None)
        if image_id:
            return str(image_id)
        raise ValueError(f"media component lacks file source: {comp!r}")

    async def _event_from_onebot(self, data: dict[str, Any]) -> Optional[Event]:
        post_type = data.get("post_type")
        if post_type == "message":
            return await self._message_event_from_onebot(data)
        if post_type == "notice":
            return await self._notice_event_from_onebot(data)
        if post_type == "request":
            return await self._request_event_from_onebot(data)
        return None

    async def _message_event_from_onebot(self, data: dict[str, Any]):
        message_type = data.get("message_type")
        chain = await self._message_from_onebot(
            data.get("message") or [],
            message_id=data.get("message_id"),
            timestamp=data.get("time"),
            group_id=data.get("group_id"),
            sender_id=data.get("user_id"),
        )
        if message_type == "group":
            group_id = int(data["group_id"])
            group = await self._group_from_event(data)
            sender = await self._member_from_event(data, group=group)
            return GroupMessage(sender=sender, messageChain=chain)
        if message_type == "private":
            sub_type = data.get("sub_type")
            sender_data = data.get("sender") or {}
            user_id = int(data.get("user_id"))
            if sub_type == "group" and data.get("group_id"):
                group = await self._group_from_event(data)
                sender = await self._member_from_event(data, group=group)
                return TempMessage(sender=sender, messageChain=chain)
            return FriendMessage(sender=Friend(id=user_id, nickname=sender_data.get("nickname")), messageChain=chain)
        return None

    async def _message_event_from_get_msg(self, data: dict[str, Any], group: Optional[int]):
        group_id = int(data.get("group_id") or group or 0)
        sender_id = int(data.get("sender", {}).get("user_id") or data.get("user_id") or 0)
        chain = await self._message_from_onebot(
            data.get("message") or [],
            message_id=data.get("message_id"),
            timestamp=data.get("time"),
            group_id=group_id,
            sender_id=sender_id,
        )
        if group_id:
            group_obj = await self.get_group(group_id)
            sender = await self._member_from_event({"sender": data.get("sender") or {}, "user_id": sender_id, "group_id": group_id}, group=group_obj)
            return GroupMessage(sender=sender, messageChain=chain)
        return FriendMessage(sender=Friend(id=sender_id), messageChain=chain)

    async def _hydrate_event_quotes(self, event: Any) -> None:
        if not isinstance(event, MessageEvent):
            return
        chain = getattr(event, "message_chain", None)
        if chain is None:
            return

        group_id = None
        if isinstance(event, GroupMessage):
            group_id = event.group.id
        elif isinstance(event, TempMessage):
            group_id = event.group.id

        quoted_events: dict[int, MessageEvent] = {}
        for comp in chain:
            if not isinstance(comp, Quote) or comp.id is None or comp.id <= 0:
                continue
            quoted_event = quoted_events.get(comp.id)
            if quoted_event is None:
                try:
                    quoted_event = (await self.message_from_id(comp.id, group_id)).data
                except Exception:
                    logger.warning(
                        "napcat quote lookup failed message_id=%s group_id=%s",
                        comp.id,
                        group_id,
                        exc_info=True,
                    )
                    continue
                quoted_events[comp.id] = quoted_event
            self._apply_quoted_event_to_quote(comp, quoted_event)

    def _apply_quoted_event_to_quote(self, quote: Quote, event: MessageEvent) -> None:
        sender = getattr(event, "sender", None)
        if sender is not None:
            quote.sender_id = int(sender.id)
        chain = getattr(event, "message_chain", None)
        if chain is not None:
            quote.origin = chain
        if isinstance(event, GroupMessage):
            quote.group_id = event.group.id
            quote.target_id = event.group.id
        elif isinstance(event, TempMessage):
            quote.group_id = event.group.id
            quote.target_id = event.group.id
        elif isinstance(event, FriendMessage):
            quote.group_id = 0
            quote.target_id = self.qq if quote.sender_id != self.qq else event.sender.id

    async def _notice_event_from_onebot(self, data: dict[str, Any]):
        notice_type = data.get("notice_type")
        group_id = data.get("group_id")
        if notice_type == "group_recall":
            group = self._group_from_event_sync(data)
            operator = None
            if data.get("operator_id"):
                operator = self._member_placeholder(group, int(data["operator_id"]))
            return GroupRecallEvent(
                authorId=int(data.get("user_id", 0)),
                messageId=int(data.get("message_id", 0)),
                time=datetime.fromtimestamp(int(data.get("time", time.time()))),
                group=group,
                operator=operator,
            )
        if notice_type == "group_increase":
            group = self._group_from_event_sync(data)
            member = self._member_placeholder(group, int(data.get("user_id")))
            invitor = None
            if data.get("operator_id"):
                invitor = self._member_placeholder(group, int(data.get("operator_id")))
            return MemberJoinEvent(member=member, invitor=invitor)
        if notice_type == "group_ban" and data.get("sub_type") == "lift_ban":
            group = self._group_from_event_sync(data)
            member = self._member_placeholder(group, int(data.get("user_id")))
            operator = None
            if data.get("operator_id"):
                operator = self._member_placeholder(group, int(data.get("operator_id")))
            return MemberUnmuteEvent(member=member, operator=operator)
        if notice_type == "group_card":
            group = self._group_from_event_sync(data)
            member = self._member_placeholder(group, int(data.get("user_id")), name=data.get("card_new") or "")
            return MemberCardChangeEvent(
                origin=data.get("card_old", ""),
                current=data.get("card_new", ""),
                member=member,
            )
        if notice_type == "notify" and data.get("sub_type") == "poke":
            subject = Subject(id=int(group_id), kind="Group") if group_id else Subject(id=int(data.get("user_id")), kind="Friend")
            return NudgeEvent(
                fromId=int(data.get("user_id", 0)),
                target=int(data.get("target_id", 0)),
                subject=subject,
                action="戳了戳",
                suffix="",
            )
        return None

    async def _request_event_from_onebot(self, data: dict[str, Any]):
        if data.get("request_type") != "group":
            return None
        group_id = int(data.get("group_id", 0))
        group = self._group_cache.get(group_id)
        flag = data.get("flag", "")
        event_id = int(flag) if str(flag).isdigit() else 0
        event = MemberJoinRequestEvent(
            eventId=event_id,
            fromId=int(data.get("user_id", 0)),
            groupId=group_id,
            groupName=group.name if group else str(group_id),
            nick=data.get("comment", ""),
            message=data.get("comment", ""),
        )
        event.onebot_flag = str(flag)
        return event

    async def _message_from_onebot(
        self,
        segments,
        *,
        message_id: Optional[int],
        timestamp: Optional[int],
        group_id: Optional[int] = None,
        sender_id: Optional[int] = None,
    ) -> MessageChain:
        if isinstance(segments, str):
            components = [Plain(segments)]
        else:
            components = []
            for segment in segments:
                component = await self._segment_to_component(segment, group_id=group_id, sender_id=sender_id)
                if component is None:
                    continue
                if isinstance(component, list):
                    components.extend(component)
                else:
                    components.append(component)
        source = Source(
            id=int(message_id or -1),
            time=datetime.fromtimestamp(int(timestamp or time.time())),
        )
        return MessageChain([source, *components])

    async def _segment_to_component(self, segment: dict[str, Any], *, group_id: Optional[int], sender_id: Optional[int]):
        typ = segment.get("type")
        data = segment.get("data") or {}
        if typ == "text":
            return Plain(data.get("text", ""))
        if typ == "at":
            qq = data.get("qq")
            if qq == "all":
                return AtAll()
            return At(target=int(qq), display=data.get("name"))
        if typ == "face":
            raw_id = data.get("id") or data.get("face_id")
            return Face(faceId=int(raw_id), name=data.get("name")) if raw_id is not None else None
        if typ == "reply":
            quoted_id = int(data.get("id", 0))
            reply_group_id = self._int_or_none(data.get("group_id")) or group_id
            reply_sender_id = (
                self._int_or_none(data.get("qq"))
                or self._int_or_none(data.get("user_id"))
                or self._int_or_none(data.get("sender_id"))
                or self._int_or_none(data.get("senderId"))
            )
            reply_origin = MessageChain([])
            if data.get("text"):
                reply_origin = MessageChain([Plain(str(data.get("text")))])
            return Quote(
                id=quoted_id,
                groupId=reply_group_id,
                senderId=reply_sender_id,
                targetId=reply_group_id or self.qq,
                origin=reply_origin,
            )
        if typ == "image":
            if data.get("emoji_id") or data.get("emoji_package_id"):
                return MarketFace(
                    id=int(data.get("emoji_id") or 0),
                    name=data.get("summary"),
                    image_id=data.get("file") or data.get("url"),
                    url=data.get("url"),
                )
            return Image(imageId=data.get("file"), url=data.get("url"))
        if typ == "record":
            return Voice(voiceId=data.get("file"), url=data.get("url"), path=data.get("path"))
        if typ == "video":
            return ShortVideo(file=data.get("file"), url=data.get("url"))
        if typ == "file":
            return File(id=data.get("file_id") or data.get("file") or "", name=data.get("file") or "", size=int(data.get("file_size") or 0))
        if typ == "json":
            return App(content=json.dumps(data.get("data"), ensure_ascii=False) if not isinstance(data.get("data"), str) else data.get("data"))
        if typ == "forward":
            return Forward(nodeList=[])
        return Plain(str(segment))

    async def _group_from_event(self, data: dict[str, Any]) -> Group:
        group_id = int(data["group_id"])
        return self._group_from_event_sync(data)

    def _group_from_event_sync(self, data: dict[str, Any]) -> Group:
        group_id = int(data["group_id"])
        group = self._group_cache.get(group_id)
        if group is not None:
            return group
        sender = data.get("sender") or {}
        group = Group(
            id=group_id,
            name=str(data.get("group_name") or sender.get("group_name") or group_id),
            permission=Permission.Member,
        )
        self._group_cache[group.id] = group
        return group

    def _member_placeholder(self, group: Group, user_id: int, *, name: Optional[str] = None) -> GroupMember:
        member = GroupMember(
            id=user_id,
            memberName=name or str(user_id),
            permission=Permission.Member,
            group=group,
        )
        self._member_cache[(group.id, member.id)] = member
        return member

    async def _member_from_event(self, data: dict[str, Any], *, group: Group) -> GroupMember:
        sender = data.get("sender") or {}
        user_id = int(data.get("user_id") or sender.get("user_id"))
        member = GroupMember(
            id=user_id,
            memberName=sender.get("card") or sender.get("nickname") or str(user_id),
            permission=self._permission_from_role(sender.get("role")),
            group=group,
            specialTitle=sender.get("title") or "",
        )
        self._member_cache[(group.id, member.id)] = member
        return member

    async def _member_from_onebot(self, data: dict[str, Any], *, group_id: int) -> GroupMember:
        group = self._group_cache.get(group_id) or await self.get_group(group_id)
        member = GroupMember(
            id=int(data.get("user_id")),
            memberName=data.get("card") or data.get("nickname") or str(data.get("user_id")),
            permission=self._permission_from_role(data.get("role")),
            group=group,
            specialTitle=data.get("title") or "",
            joinTimestamp=datetime.fromtimestamp(int(data.get("join_time") or 0)),
            lastSpeakTimestamp=datetime.fromtimestamp(int(data.get("last_sent_time") or 0)),
            muteTimeRemaining=self._mute_time_remaining(data.get("shut_up_timestamp")),
        )
        return member

    def _group_from_onebot(self, data: dict[str, Any]) -> Group:
        permission = self._permission_from_role(data.get("role"))
        return Group(
            id=int(data.get("group_id")),
            name=data.get("group_name") or str(data.get("group_id")),
            permission=permission,
        )

    def _permission_from_role(self, role: Optional[str]) -> Permission:
        if role == "owner":
            return Permission.Owner
        if role == "admin":
            return Permission.Administrator
        return Permission.Member

    def _mute_time_remaining(self, shut_up_timestamp: Any) -> int:
        timestamp = int(shut_up_timestamp or 0)
        if timestamp <= 0:
            return 0
        return max(0, timestamp - int(time.time()))

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_request_approved(self, operate: RespOperate) -> bool:
        return operate == RespOperate.ALLOW or bool(getattr(operate, "value", 0) & RespOperate.ALLOW.value)


def get_config_value(config, name: str, default=None):
    env_name = name.upper()
    if env_name in os.environ:
        return os.environ[env_name]
    return getattr(config, name, default)
