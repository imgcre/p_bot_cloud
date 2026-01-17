from dataclasses import dataclass
from typing import Optional

from utilities import AchvEnum

@dataclass
class EffectiveSpeechEvent():
    ...

@dataclass
class AchvObtainedEvent():
    e: AchvEnum

@dataclass
class AchvRemovedEvent():
    e: AchvEnum

@dataclass
class ViolationEvent():
    member_id: int
    hint: Optional[str]
    count: int
    dec: bool = False

@dataclass
class LiveStartedEvent():
    ...

@dataclass
class LiveStoppedEvent():
    ...