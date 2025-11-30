from abc import ABC

from ..generic import DictionaryObject
from ..generic._base import (
    NameObject,
    TextStringObject,
)


class Action(DictionaryObject, ABC):
    def __init__(self) -> None:
        super().__init__()
        self[NameObject("/Type")] = NameObject("/Action")


class JavaScript(Action):
    def __init__(self, JS: str) -> None:
        super().__init__()
        self[NameObject("/S")] = NameObject("/JavaScript")
        self[NameObject("/JS")] = TextStringObject(JS)
