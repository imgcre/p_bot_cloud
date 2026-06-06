from __future__ import annotations

from typing import Optional


def patch_mirai_models() -> None:
    """Patch local yiri-mirai models that this project historically extended."""
    import mirai
    import mirai.models.events as events
    import mirai.models.message as message

    if not hasattr(message, "MarketFace"):
        class MarketFace(message.MessageComponent):
            type: str = "MarketFace"
            id: Optional[int] = None
            name: Optional[str] = None
            image_id: Optional[str] = None
            url: Optional[str] = None

        message.MarketFace = MarketFace
        if hasattr(message, "__all__") and "MarketFace" not in message.__all__:
            message.__all__.append("MarketFace")

    if not hasattr(message, "ShortVideo"):
        class ShortVideo(message.MessageComponent):
            type: str = "ShortVideo"
            file: Optional[str] = None
            url: Optional[str] = None
            path: Optional[str] = None
            base64: Optional[str] = None

        message.ShortVideo = ShortVideo
        if hasattr(message, "__all__") and "ShortVideo" not in message.__all__:
            message.__all__.append("ShortVideo")

    for name in ("MarketFace", "ShortVideo"):
        if not hasattr(mirai, name):
            setattr(mirai, name, getattr(message, name))

    if not hasattr(events.GroupMessage, "member_id"):
        events.GroupMessage.member_id = property(lambda self: self.sender.id)
    if not hasattr(events.GroupMessage, "from_id"):
        events.GroupMessage.from_id = property(lambda self: self.sender.id)
    if not hasattr(events.GroupMessage, "group_id"):
        events.GroupMessage.group_id = property(lambda self: self.group.id)
    if not hasattr(events.TempMessage, "member_id"):
        events.TempMessage.member_id = property(lambda self: self.sender.id)
    if not hasattr(events.TempMessage, "from_id"):
        events.TempMessage.from_id = property(lambda self: self.sender.id)
    if not hasattr(events.TempMessage, "group_id"):
        events.TempMessage.group_id = property(lambda self: self.group.id)


patch_mirai_models()
