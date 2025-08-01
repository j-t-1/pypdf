# Copyright (c) 2006, Mathieu Fenniak
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# * Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
# * The name of the author may not be used to endorse or promote products
# derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


"""
Implementation of stream filters for PDF.

See TABLE H.1 Abbreviations for standard filter names
"""
__author__ = "Mathieu Fenniak"
__author_email__ = "biziqe@mathieu.fenniak.net"

import math
import os
import shutil
import struct
import subprocess
import zlib
from base64 import a85decode
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional, Union, cast

from ._codecs._codecs import LzwCodec as _LzwCodec
from ._utils import (
    WHITESPACES_AS_BYTES,
    deprecate,
    deprecate_with_replacement,
    deprecation_no_replacement,
    logger_warning,
)
from .constants import CcittFaxDecodeParameters as CCITT
from .constants import FilterTypeAbbreviations as FTA
from .constants import FilterTypes as FT
from .constants import ImageAttributes as IA
from .constants import LzwFilterParameters as LZW
from .constants import StreamAttributes as SA
from .errors import DependencyError, DeprecationError, PdfReadError, PdfStreamError
from .generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    IndirectObject,
    NullObject,
    StreamObject,
    is_null_or_none,
)


def decompress(data: bytes) -> bytes:
    """
    Decompress the given data using zlib.

    Attempts to decompress the input data using zlib.
    If the decompression fails due to a zlib error, it falls back
    to using a decompression object with a larger window size.

    Args:
        data: The input data to be decompressed.

    Returns:
        The decompressed data.

    """
    try:
        return zlib.decompress(data)
    except zlib.error:
        try:
            # For larger files, use decompression object to enable buffered reading
            return zlib.decompressobj().decompress(data)
        except zlib.error:
            # First quick approach for known issue with faulty added bytes to the
            # tail of the encoded stream from early Adobe Distiller or Pitstop versions
            # with CR char as the default line separator (assumed by reverse engeneering)
            # that breaks the decoding process in the end.
            #
            # Try first to cut off some of the tail byte by byte, however limited to not
            # iterate through too many loops and kill the performance for large streams,
            # to then allow the final fallback to run. Added this intermediate attempt,
            # because starting from the head of the stream byte by byte kills completely
            # the performace for large streams (e.g. 6 MB) with the tail-byte-issue
            # and takes ages. This solution is really fast:
            max_tail_cut_off_bytes: int = 8
            for i in range(1, min(max_tail_cut_off_bytes + 1, len(data))):
                try:
                    return zlib.decompressobj().decompress(data[:-i])
                except zlib.error:
                    pass
            # If still failing, then try with increased window size
            d = zlib.decompressobj(zlib.MAX_WBITS | 32)
            result_str = b""
            for b in [data[i : i + 1] for i in range(len(data))]:
                try:
                    result_str += d.decompress(b)
                except zlib.error:
                    pass
            return result_str


class FlateDecode:
    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decode data which is flate-encoded.

        Args:
          data: flate-encoded data.
          decode_parms: a dictionary of values, understanding the
            "/Predictor":<int> key only

        Returns:
          The flate-decoded data.

        Raises:
          PdfReadError:

        """
        if isinstance(decode_parms, ArrayObject):
            raise DeprecationError("decode_parms as ArrayObject is deprecated")

        str_data = decompress(data)
        predictor = 1

        if decode_parms:
            try:
                predictor = decode_parms.get("/Predictor", 1)
            except (AttributeError, TypeError):  # Type Error is NullObject
                pass  # Usually an array with a null object was read
        # predictor 1 == no predictor
        if predictor != 1:
            # /Columns, the number of samples in each row, has a default value of 1;
            # §7.4.4.3, ISO 32000.
            DEFAULT_BITS_PER_COMPONENT = 8
            try:
                columns = cast(int, decode_parms[LZW.COLUMNS].get_object())  # type: ignore
            except (TypeError, KeyError):
                columns = 1
            try:
                colors = cast(int, decode_parms[LZW.COLORS].get_object())  # type: ignore
            except (TypeError, KeyError):
                colors = 1
            try:
                bits_per_component = cast(
                    int,
                    decode_parms[LZW.BITS_PER_COMPONENT].get_object(),  # type: ignore
                )
            except (TypeError, KeyError):
                bits_per_component = DEFAULT_BITS_PER_COMPONENT

            # PNG predictor can vary by row and so is the lead byte on each row
            rowlength = (
                math.ceil(columns * colors * bits_per_component / 8) + 1
            )  # number of bytes

            # TIFF prediction:
            if predictor == 2:
                rowlength -= 1  # remove the predictor byte
                bpp = rowlength // columns
                str_data = bytearray(str_data)
                for i in range(len(str_data)):
                    if i % rowlength >= bpp:
                        str_data[i] = (str_data[i] + str_data[i - bpp]) % 256
                str_data = bytes(str_data)
            # PNG prediction:
            elif 10 <= predictor <= 15:
                str_data = FlateDecode._decode_png_prediction(
                    str_data, columns, rowlength
                )
            else:
                raise PdfReadError(f"Unsupported flatedecode predictor {predictor!r}")
        return str_data

    @staticmethod
    def _decode_png_prediction(data: bytes, columns: int, rowlength: int) -> bytes:
        # PNG prediction can vary from row to row
        if (remainder := len(data) % rowlength) != 0:
            logger_warning("Image data is not rectangular. Adding padding.", __name__)
            data += b"\x00" * (rowlength - remainder)
            assert len(data) % rowlength == 0
        output = []
        prev_rowdata = (0,) * rowlength
        bpp = (rowlength - 1) // columns  # recomputed locally to not change params
        for row in range(0, len(data), rowlength):
            rowdata: list[int] = list(data[row : row + rowlength])
            filter_byte = rowdata[0]

            if filter_byte == 0:
                # PNG None Predictor
                pass
            elif filter_byte == 1:
                # PNG Sub Predictor
                for i in range(bpp + 1, rowlength):
                    rowdata[i] = (rowdata[i] + rowdata[i - bpp]) % 256
            elif filter_byte == 2:
                # PNG Up Predictor
                for i in range(1, rowlength):
                    rowdata[i] = (rowdata[i] + prev_rowdata[i]) % 256
            elif filter_byte == 3:
                # PNG Average Predictor
                for i in range(1, bpp + 1):
                    floor = prev_rowdata[i] // 2
                    rowdata[i] = (rowdata[i] + floor) % 256
                for i in range(bpp + 1, rowlength):
                    left = rowdata[i - bpp]
                    floor = (left + prev_rowdata[i]) // 2
                    rowdata[i] = (rowdata[i] + floor) % 256
            elif filter_byte == 4:
                # PNG Paeth Predictor
                for i in range(1, bpp + 1):
                    rowdata[i] = (rowdata[i] + prev_rowdata[i]) % 256
                for i in range(bpp + 1, rowlength):
                    left = rowdata[i - bpp]
                    up = prev_rowdata[i]
                    up_left = prev_rowdata[i - bpp]

                    p = left + up - up_left
                    dist_left = abs(p - left)
                    dist_up = abs(p - up)
                    dist_up_left = abs(p - up_left)

                    if dist_left <= dist_up and dist_left <= dist_up_left:
                        paeth = left
                    elif dist_up <= dist_up_left:
                        paeth = up
                    else:
                        paeth = up_left

                    rowdata[i] = (rowdata[i] + paeth) % 256
            else:
                raise PdfReadError(
                    f"Unsupported PNG filter {filter_byte!r}"
                )  # pragma: no cover
            prev_rowdata = tuple(rowdata)
            output.extend(rowdata[1:])
        return bytes(output)

    @staticmethod
    def encode(data: bytes, level: int = -1) -> bytes:
        """
        Compress the input data using zlib.

        Args:
            data: The data to be compressed.
            level: See https://docs.python.org/3/library/zlib.html#zlib.compress

        Returns:
            The compressed data.

        """
        return zlib.compress(data, level)


class ASCIIHexDecode:
    """
    The ASCIIHexDecode filter decodes data that has been encoded in ASCII
    hexadecimal form into a base-7 ASCII format.
    """

    @staticmethod
    def decode(
        data: Union[str, bytes],
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decode an ASCII-Hex encoded data stream.

        Args:
          data: a str sequence of hexadecimal-encoded values to be
            converted into a base-7 ASCII string
          decode_parms: this filter does not use parameters.

        Returns:
          A string conversion in base-7 ASCII, where each of its values
          v is such that 0 <= ord(v) <= 127.

        Raises:
          PdfStreamError:

        """
        if isinstance(data, str):
            data = data.encode()
        retval = b""
        hex_pair = b""
        index = 0
        while True:
            if index >= len(data):
                logger_warning(
                    "missing EOD in ASCIIHexDecode, check if output is OK", __name__
                )
                break  # Reached end of string without an EOD
            char = data[index : index + 1]
            if char == b">":
                break
            if char.isspace():
                index += 1
                continue
            hex_pair += char
            if len(hex_pair) == 2:
                retval += bytes((int(hex_pair, base=16),))
                hex_pair = b""
            index += 1
        # If the filter encounters the EOD marker after reading
        # an odd number of hexadecimal digits,
        # it shall behave as if a 0 (zero) followed the last digit.
        # For every even number of hexadecimal digits, hex_pair is reset to b"".
        if hex_pair != b"":
            hex_pair += b"0"
            retval += bytes((int(hex_pair, base=16),))
        return retval


class RunLengthDecode:
    """
    The RunLengthDecode filter decodes data that has been encoded in a
    simple byte-oriented format based on run length.
    The encoded data is a sequence of runs, where each run consists of
    a length byte followed by 1 to 128 bytes of data. If the length byte is
    in the range 0 to 127,
    the following length + 1 (1 to 128) bytes are copied literally during
    decompression.
    If length is in the range 129 to 255, the following single byte is to be
    copied 257 − length (2 to 128) times during decompression. A length value
    of 128 denotes EOD.
    """

    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decode a run length encoded data stream.

        Args:
          data: a bytes sequence of length/data
          decode_parms: this filter does not use parameters.

        Returns:
          A bytes decompressed sequence.

        Raises:
          PdfStreamError:

        """
        lst = []
        index = 0
        while True:
            if index >= len(data):
                logger_warning(
                    "missing EOD in RunLengthDecode, check if output is OK", __name__
                )
                break  # Reached end of string without an EOD
            length = data[index]
            index += 1
            if length == 128:
                data_length = len(data)
                if index < data_length:
                    # We should first check, if we have an inner stream from a multi-encoded
                    # stream with a faulty trailing newline that we can decode properly.
                    # We will just ignore the last byte and raise a warning ...
                    if (index == data_length - 1) and (data[index : index+1] == b"\n"):
                        logger_warning(
                            "Found trailing newline in stream data, check if output is OK", __name__
                        )
                        break
                    raise PdfStreamError("Early EOD in RunLengthDecode")
                break
            if length < 128:
                length += 1
                lst.append(data[index : (index + length)])
                index += length
            else:  # >128
                length = 257 - length
                lst.append(bytes((data[index],)) * length)
                index += 1
        return b"".join(lst)


class LZWDecode:
    class Decoder:
        STOP = 257
        CLEARDICT = 256

        def __init__(self, data: bytes) -> None:
            self.data = data

        def decode(self) -> bytes:
            return _LzwCodec().decode(self.data)

    @staticmethod
    def _decodeb(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decode an LZW encoded data stream.

        Args:
          data: ``bytes`` or ``str`` text to decode.
          decode_parms: a dictionary of parameter values.

        Returns:
          decoded data.

        """
        # decode_parms is unused here
        return LZWDecode.Decoder(data).decode()

    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> str:  # deprecated
        """
        Decode an LZW encoded data stream.

        Args:
          data: ``bytes`` or ``str`` text to decode.
          decode_parms: a dictionary of parameter values.

        Returns:
          decoded data.

        """
        # decode_parms is unused here
        deprecate("LZWDecode.decode will return bytes instead of str in pypdf 6.0.0")
        return LZWDecode.Decoder(data).decode().decode("latin-1")


class ASCII85Decode:
    """Decodes string ASCII85-encoded data into a byte format."""

    @staticmethod
    def decode(
        data: Union[str, bytes],
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decode an Ascii85 encoded data stream.

        Args:
          data: ``bytes`` or ``str`` text to decode.
          decode_parms: this filter does not use parameters.

        Returns:
          decoded data.

        """
        if isinstance(data, str):
            data = data.encode()
        data = data.strip(WHITESPACES_AS_BYTES)
        try:
            return a85decode(data, adobe=True, ignorechars=WHITESPACES_AS_BYTES)
        except ValueError as error:
            if error.args[0] == "Ascii85 encoded byte sequences must end with b'~>'":
                logger_warning("Ignoring missing Ascii85 end marker.", __name__)
                return a85decode(data, adobe=False, ignorechars=WHITESPACES_AS_BYTES)
            raise


class DCTDecode:
    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decompresses data encoded using a DCT (discrete cosine transform)
        technique based on the JPEG standard (IS0/IEC 10918),
        reproducing image sample data that approximates the original data.

        Args:
          data: text to decode.
          decode_parms: this filter does not use parameters.

        Returns:
          decoded data.

        """
        return data


class JPXDecode:
    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        """
        Decompresses data encoded using the wavelet-based JPEG 2000 standard,
        reproducing the original image data.

        Args:
          data: text to decode.
          decode_parms: a dictionary of parameter values.

        Returns:
          decoded data.

        """
        # decode_parms: this filter does not use parameters
        return data


@dataclass
class CCITTParameters:
    """§7.4.6, optional parameters for the CCITTFaxDecode filter."""

    K: int = 0
    columns: int = 0
    rows: int = 0
    EndOfBlock: Union[int, None] = None
    EndOfLine: Union[int, None] = None
    EncodedByteAlign: Union[int, None] = None
    DamagedRowsBeforeError: Union[int, None] = None

    @property
    def group(self) -> int:
        if self.K < 0:
            # Pure two-dimensional encoding (Group 4)
            CCITTgroup = 4
        else:
            # K == 0: Pure one-dimensional encoding (Group 3, 1-D)
            # K > 0: Mixed one- and two-dimensional encoding (Group 3, 2-D)
            CCITTgroup = 3
        return CCITTgroup


def __create_old_class_instance(
    K: int = 0,
    columns: int = 0,
    rows: int = 0
) -> CCITTParameters:
    deprecate_with_replacement("CCITParameters", "CCITTParameters", "6.0.0")
    return CCITTParameters(K, columns, rows)


# Create an alias for the old class name
CCITParameters = __create_old_class_instance


class CCITTFaxDecode:
    """
    §7.4.6, CCITTFaxDecode filter (ISO 32000).

    Either Group 3 or Group 4 CCITT facsimile (fax) encoding.
    CCITT encoding is bit-oriented, not byte-oriented.

    §7.4.6, optional parameters for the CCITTFaxDecode filter.
    """

    @staticmethod
    def _get_parameters(
        parameters: Union[None, ArrayObject, DictionaryObject, IndirectObject],
        rows: Union[int, IndirectObject],
    ) -> CCITTParameters:
        # §7.4.6, optional parameters for the CCITTFaxDecode filter
        k = 0
        columns = 1728
        if parameters:
            parameters_unwrapped = cast(
                Union[ArrayObject, DictionaryObject], parameters.get_object()
            )
            if isinstance(parameters_unwrapped, ArrayObject):
                for decode_parm in parameters_unwrapped:
                    if CCITT.COLUMNS in decode_parm:
                        columns = decode_parm[CCITT.COLUMNS].get_object()
                    if CCITT.K in decode_parm:
                        k = decode_parm[CCITT.K].get_object()
            else:
                if CCITT.COLUMNS in parameters_unwrapped:
                    columns = parameters_unwrapped[CCITT.COLUMNS].get_object()  # type: ignore
                if CCITT.K in parameters_unwrapped:
                    k = parameters_unwrapped[CCITT.K].get_object()  # type: ignore

        return CCITTParameters(K=k, columns=columns, rows=int(rows))

    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        height: int = 0,
        **kwargs: Any,
    ) -> bytes:
        # decode_parms is unused here
        if isinstance(decode_parms, ArrayObject):  # deprecated
            deprecation_no_replacement(
                "decode_parms being an ArrayObject", removed_in="3.15.5"
            )
        params = CCITTFaxDecode._get_parameters(decode_parms, height)

        img_size = len(data)
        tiff_header_struct = "<2shlh" + "hhll" * 8 + "h"
        tiff_header = struct.pack(
            tiff_header_struct,
            b"II",  # Byte order indication: Little endian
            42,  # Version number (always 42)
            8,  # Offset to first IFD
            8,  # Number of tags in IFD
            256,
            4,
            1,
            params.columns,  # ImageWidth, LONG, 1, width
            257,
            4,
            1,
            params.rows,  # ImageLength, LONG, 1, length
            258,
            3,
            1,
            1,  # BitsPerSample, SHORT, 1, 1
            259,
            3,
            1,
            params.group,  # Compression, SHORT, 1, 4 = CCITT Group 4 fax encoding
            262,
            3,
            1,
            0,  # Thresholding, SHORT, 1, 0 = WhiteIsZero
            273,
            4,
            1,
            struct.calcsize(
                tiff_header_struct
            ),  # StripOffsets, LONG, 1, length of header
            278,
            4,
            1,
            params.rows,  # RowsPerStrip, LONG, 1, length
            279,
            4,
            1,
            img_size,  # StripByteCounts, LONG, 1, size of image
            0,  # last IFD
        )

        return tiff_header + data


JBIG2DEC_BINARY = shutil.which("jbig2dec")


class JBIG2Decode:
    @staticmethod
    def decode(
        data: bytes,
        decode_parms: Optional[DictionaryObject] = None,
        **kwargs: Any,
    ) -> bytes:
        if JBIG2DEC_BINARY is None:
            raise DependencyError("jbig2dec binary is not available.")

        with TemporaryDirectory() as tempdir:
            directory = Path(tempdir)
            paths: list[Path] = []

            if decode_parms and "/JBIG2Globals" in decode_parms:
                jbig2_globals = decode_parms["/JBIG2Globals"]
                if not is_null_or_none(jbig2_globals) and not is_null_or_none(pointer := jbig2_globals.get_object()):
                    assert pointer is not None, "mypy"
                    if isinstance(pointer, StreamObject):
                        path = directory.joinpath("globals.jbig2")
                        path.write_bytes(pointer.get_data())
                        paths.append(path)

            path = directory.joinpath("image.jbig2")
            path.write_bytes(data)
            paths.append(path)

            environment = os.environ.copy()
            environment["LC_ALL"] = "C"
            result = subprocess.run(  # noqa: S603
                [JBIG2DEC_BINARY, "--embedded", "--format", "png", "--output", "-", *paths],
                capture_output=True,
                env=environment,
            )
            if b"unrecognized option '--embedded'" in result.stderr:
                raise DependencyError("jbig2dec>=0.15 is required.")
            if result.stderr:
                for line in result.stderr.decode("utf-8").splitlines():
                    logger_warning(line, __name__)
            if result.returncode != 0:
                raise PdfStreamError(f"Unable to decode JBIG2 data. Exit code: {result.returncode}")
        return result.stdout

    @staticmethod
    def _is_binary_compatible() -> bool:
        if not JBIG2DEC_BINARY:  # pragma: no cover
            return False
        result = subprocess.run(  # noqa: S603
            [JBIG2DEC_BINARY, "--version"],
            capture_output=True,
            text=True,
        )
        version = result.stdout.split(" ", maxsplit=1)[1]

        from ._utils import Version  # noqa: PLC0415
        return Version(version) >= Version("0.15")


def decode_stream_data(stream: Any) -> bytes:
    """
    Decode the stream data based on the specified filters.

    This function decodes the stream data using the filters provided in the
    stream.

    Args:
        stream: The input stream object containing the data and filters.

    Returns:
        The decoded stream data.

    Raises:
        NotImplementedError: If an unsupported filter type is encountered.

    """
    filters = stream.get(SA.FILTER, ())
    if isinstance(filters, IndirectObject):
        filters = cast(ArrayObject, filters.get_object())
    if not isinstance(filters, ArrayObject):
        # We have a single filter instance
        filters = (filters,)
    decode_parms = stream.get(SA.DECODE_PARMS, ({},) * len(filters))
    if not isinstance(decode_parms, (list, tuple)):
        decode_parms = (decode_parms,)
    data: bytes = stream._data
    # If there is not data to decode we should not try to decode the data.
    if not data:
        return data
    for filter_name, params in zip(filters, decode_parms):
        if isinstance(params, NullObject):
            params = {}
        if filter_name in (FT.ASCII_HEX_DECODE, FTA.AHx):
            data = ASCIIHexDecode.decode(data)
        elif filter_name in (FT.ASCII_85_DECODE, FTA.A85):
            data = ASCII85Decode.decode(data)
        elif filter_name in (FT.LZW_DECODE, FTA.LZW):
            data = LZWDecode._decodeb(data, params)
        elif filter_name in (FT.FLATE_DECODE, FTA.FL):
            data = FlateDecode.decode(data, params)
        elif filter_name in (FT.RUN_LENGTH_DECODE, FTA.RL):
            data = RunLengthDecode.decode(data)
        elif filter_name == FT.CCITT_FAX_DECODE:
            height = stream.get(IA.HEIGHT, ())
            data = CCITTFaxDecode.decode(data, params, height)
        elif filter_name == FT.DCT_DECODE:
            data = DCTDecode.decode(data)
        elif filter_name == FT.JPX_DECODE:
            data = JPXDecode.decode(data)
        elif filter_name == FT.JBIG2_DECODE:
            data = JBIG2Decode.decode(data, params)
        elif filter_name == "/Crypt":
            if "/Name" in params or "/Type" in params:
                raise NotImplementedError(
                    "/Crypt filter with /Name or /Type not supported yet"
                )
        else:
            raise NotImplementedError(f"Unsupported filter {filter_name}")
    return data


def _xobj_to_image(x_object_obj: dict[str, Any]) -> tuple[Optional[str], bytes, Any]:
    """
    Users need to have the pillow package installed.

    It's unclear if pypdf will keep this function here, hence it's private.
    It might get removed at any point.

    Args:
      x_object_obj:

    Returns:
        Tuple[file extension, bytes, PIL.Image.Image]

    """
    from ._xobj_image_helpers import (  # noqa: PLC0415
        Image,
        UnidentifiedImageError,
        _apply_decode,
        _extended_image_frombytes,
        _get_mode_and_invert_color,
        _handle_flate,
        _handle_jpx,
    )

    def _apply_alpha(
        img: Image.Image,
        x_object_obj: dict[str, Any],
        obj_as_text: str,
        image_format: str,
        extension: str,
    ) -> tuple[Image.Image, str, str]:
        alpha = None
        if IA.S_MASK in x_object_obj:  # add alpha channel
            alpha = _xobj_to_image(x_object_obj[IA.S_MASK])[2]
            if img.size != alpha.size:
                logger_warning(
                    f"image and mask size not matching: {obj_as_text}", __name__
                )
            else:
                # TODO : implement mask
                if alpha.mode != "L":
                    alpha = alpha.convert("L")
                if img.mode == "P":
                    img = img.convert("RGB")
                elif img.mode == "1":
                    img = img.convert("L")
                img.putalpha(alpha)
            if "JPEG" in image_format:
                extension = ".jp2"
                image_format = "JPEG2000"
            else:
                extension = ".png"
                image_format = "PNG"
        return img, extension, image_format

    # for error reporting
    obj_as_text = (
        x_object_obj.indirect_reference.__repr__()
        if x_object_obj is None  # pragma: no cover
        else x_object_obj.__repr__()
    )

    # Get size and data
    size = (cast(int, x_object_obj[IA.WIDTH]), cast(int, x_object_obj[IA.HEIGHT]))
    data = x_object_obj.get_data()  # type: ignore
    if isinstance(data, str):  # pragma: no cover
        data = data.encode()
    if len(data) % (size[0] * size[1]) == 1 and data[-1] == 0x0A:  # ie. '\n'
        data = data[:-1]

    # Get color properties
    colors = x_object_obj.get("/Colors", 1)
    color_space: Any = x_object_obj.get("/ColorSpace", NullObject()).get_object()
    if isinstance(color_space, list) and len(color_space) == 1:
        color_space = color_space[0].get_object()

    mode, invert_color = _get_mode_and_invert_color(x_object_obj, colors, color_space)

    # Get filters
    filters = x_object_obj.get(SA.FILTER, NullObject()).get_object()
    lfilters = filters[-1] if isinstance(filters, list) else filters
    decode_parms = x_object_obj.get(SA.DECODE_PARMS, None)
    if decode_parms and isinstance(decode_parms, (tuple, list)):
        decode_parms = decode_parms[0]
    else:
        decode_parms = {}
    if not isinstance(decode_parms, dict):
        decode_parms = {}

    extension = None
    if lfilters in (FT.FLATE_DECODE, FT.RUN_LENGTH_DECODE):
        img, image_format, extension, _ = _handle_flate(
            size,
            data,
            mode,
            color_space,
            colors,
            obj_as_text,
        )
    elif lfilters in (FT.LZW_DECODE, FT.ASCII_85_DECODE, FT.CCITT_FAX_DECODE):
        # I'm not sure if the following logic is correct.
        # There might not be any relationship between the filters and the
        # extension
        if lfilters in (FT.LZW_DECODE, FT.CCITT_FAX_DECODE):
            extension = ".tiff"  # mime_type = "image/tiff"
            image_format = "TIFF"
        else:
            extension = ".png"  # mime_type = "image/png"
            image_format = "PNG"
        try:
            img = Image.open(BytesIO(data), formats=("TIFF", "PNG"))
        except UnidentifiedImageError:
            img = _extended_image_frombytes(mode, size, data)
    elif lfilters == FT.DCT_DECODE:
        img, image_format, extension = Image.open(BytesIO(data)), "JPEG", ".jpg"
        # invert_color kept unchanged
    elif lfilters == FT.JPX_DECODE:
        img, image_format, extension, invert_color = _handle_jpx(
            size, data, mode, color_space, colors
        )
    elif lfilters == FT.CCITT_FAX_DECODE:
        img, image_format, extension, invert_color = (
            Image.open(BytesIO(data), formats=("TIFF",)),
            "TIFF",
            ".tiff",
            False,
        )
    elif lfilters == FT.JBIG2_DECODE:
        img, image_format, extension, invert_color = (
            Image.open(BytesIO(data), formats=("PNG",)),
            "PNG",
            ".png",
            False,
        )
    elif mode == "CMYK":
        img, image_format, extension, invert_color = (
            _extended_image_frombytes(mode, size, data),
            "TIFF",
            ".tif",
            False,
        )
    elif mode == "":
        raise PdfReadError(f"ColorSpace field not found in {x_object_obj}")
    else:
        img, image_format, extension, invert_color = (
            _extended_image_frombytes(mode, size, data),
            "PNG",
            ".png",
            False,
        )

    img = _apply_decode(img, x_object_obj, lfilters, color_space, invert_color)
    img, extension, image_format = _apply_alpha(
        img, x_object_obj, obj_as_text, image_format, extension
    )

    if lfilters == FT.CCITT_FAX_DECODE and decode_parms.get("/BlackIs1", BooleanObject(False)).value is True:
        from PIL import ImageOps  # noqa: PLC0415
        img = ImageOps.invert(img)

    # Save image to bytes
    img_byte_arr = BytesIO()
    try:
        img.save(img_byte_arr, format=image_format)
    except OSError:  # pragma: no cover  # covered with pillow 10.3
        # in case of we convert to RGBA and then to PNG
        img1 = img.convert("RGBA")
        image_format = "PNG"
        extension = ".png"
        img_byte_arr = BytesIO()
        img1.save(img_byte_arr, format=image_format)
    data = img_byte_arr.getvalue()

    try:  # temporary try/except until other fixes of images
        img = Image.open(BytesIO(data))
    except Exception as exception:
        logger_warning(f"Failed loading image: {exception}", __name__)
        img = None  # type: ignore
    return extension, data, img
