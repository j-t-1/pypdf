"""A dataclass that captures the CTM and Text State for a tj operation"""

import math
from dataclasses import dataclass, field
from typing import Any, Union

from .. import mult, orient
from ._font import Font


@dataclass
class TextStateParams:
    """
    Text state parameters and operator values for a single text value in a
    TJ or Tj PDF operation.

    Attributes:
        txt (str): the text to be rendered.
        font (Font): font object
        font_size (int | float): font size
        Tc (float): character spacing. Defaults to 0.0.
        Tw (float): word spacing. Defaults to 0.0.
        Tz (float): horizontal scaling. Defaults to 100.0.
        TL (float): leading, vertical displacement between text lines. Defaults to 0.0.
        Ts (float): text rise. Used for super/subscripts. Defaults to 0.0.
        transform (List[float]): effective transformation matrix.
        tx (float): x cood of rendered text, i.e. self.transform[4]
        ty (float): y cood of rendered text. May differ from self.transform[5] per self.Ts.
        displaced_tx (float): x coord immediately following rendered text
        space_tx (float): tx for a space character
        font_height (float): effective font height accounting for CTM
        flip_vertical (bool): True if y axis has been inverted (i.e. if self.transform[3] < 0.)
        rotated (bool): True if the text orientation is rotated with respect to the page.

    """

    txt: str
    font: Font
    font_size: Union[int, float]
    Tc: float = 0.0
    Tw: float = 0.0
    Tz: float = 100.0
    TL: float = 0.0
    Ts: float = 0.0
    transform: list[float] = field(
        default_factory=lambda: [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    )
    tx: float = field(default=0.0, init=False)
    ty: float = field(default=0.0, init=False)
    displaced_tx: float = field(default=0.0, init=False)
    space_tx: float = field(default=0.0, init=False)
    font_height: float = field(default=0.0, init=False)
    flip_vertical: bool = field(default=False, init=False)
    rotated: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if orient(self.transform) in (90, 270):
            self.transform = mult(
                [1.0, -self.transform[1], -self.transform[2], 1.0, 0.0, 0.0],
                self.transform,
            )
            self.rotated = True
        # self.transform[0] AND self.transform[3] < 0 indicates true rotation.
        # If only self.transform[3] < 0, the y coords are simply inverted.
        if orient(self.transform) == 180 and self.transform[0] < -1e-6:
            self.transform = mult([-1.0, 0.0, 0.0, -1.0, 0.0, 0.0], self.transform)
            self.rotated = True
        self.displaced_tx = self.displaced_transform()[4]
        self.tx = self.transform[4]
        self.ty = self.render_transform()[5]
        self.space_tx = round(self.word_tx(" "), 3)
        if self.space_tx < 1e-6:
            # if the " " char is assigned 0 width (e.g. for fine tuned spacing
            # with TJ int operators a la crazyones.pdf), calculate space_tx as
            # a TD_offset of -2 * font.space_width where font.space_width is
            # the space_width calculated in _cmap.py.
            self.space_tx = round(self.word_tx("", self.font.space_width * -2), 3)
        self.font_height = self.font_size * math.sqrt(
            self.transform[1] ** 2 + self.transform[3] ** 2
        )
        # flip_vertical handles PDFs generated by Microsoft Word's "publish" command.
        self.flip_vertical = self.transform[3] < -1e-6  # inverts y axis

    def font_size_matrix(self) -> list[float]:
        """Font size matrix"""
        return [
            self.font_size * (self.Tz / 100.0),
            0.0,
            0.0,
            self.font_size,
            0.0,
            self.Ts,
        ]

    def displaced_transform(self) -> list[float]:
        """Effective transform matrix after text has been rendered."""
        return mult(self.displacement_matrix(), self.transform)

    def render_transform(self) -> list[float]:
        """Effective transform matrix accounting for font size, Tz, and Ts."""
        return mult(self.font_size_matrix(), self.transform)

    def displacement_matrix(
        self, word: Union[str, None] = None, TD_offset: float = 0.0
    ) -> list[float]:
        """
        Text displacement matrix

        Args:
            word (str, optional): Defaults to None in which case self.txt displacement is
                returned.
            TD_offset (float, optional): translation applied by TD operator. Defaults to 0.0.

        """
        word = word if word is not None else self.txt
        return [1.0, 0.0, 0.0, 1.0, self.word_tx(word, TD_offset), 0.0]

    def word_tx(self, word: str, TD_offset: float = 0.0) -> float:
        """Horizontal text displacement for any word according this text state"""
        return (
            (self.font_size * ((self.font.word_width(word) - TD_offset) / 1000.0))
            + self.Tc
            + word.count(" ") * self.Tw
        ) * (self.Tz / 100.0)

    @staticmethod
    def to_dict(inst: "TextStateParams") -> dict[str, Any]:
        """Dataclass to dict for json.dumps serialization"""
        return {k: getattr(inst, k) for k in inst.__dataclass_fields__ if k != "font"}
