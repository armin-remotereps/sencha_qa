"""Word 97–2003 (.doc) main text through the MS-DOC piece table (best effort).

1. ``olefile`` opens the file and reads the ``WordDocument`` stream's FIB.
2. A set ``fEncrypted`` (or ``fObfuscated``) is refused.
3. ``fWhichTblStm`` selects ``0Table`` / ``1Table``, where the CLX and its
   PlcPcd piece table are read (``fcClx`` / ``lcbClx`` of FibRgFcLcb97).
4. Each piece of the main text (CPs ``0 .. ccpText``) is decoded as cp1252
   (compressed) or UTF-16LE.
5. Field codes and control characters are dropped.

Every offset is bounds-checked and every loop is bounded by the stream
sizes; anything unexpected raises ``LegacyFormatUnreadable``. ``ccpText``
and the summed piece sizes may not exceed the ``WordDocument`` stream (no
huge or overlapping pieces), and the text is decoded in blocks and stops a
little past the item's character budget. Sections are
cut at paragraph marks with character-offset locations ("Word text
12 001–13 500"); the format has no page numbers without layout.
"""

from __future__ import annotations

import codecs
import io
import struct

import olefile

from knowledge_base.chunking import SectionCollector, group_by_size, range_label
from knowledge_base.exceptions import EncryptedDocument, LegacyFormatUnreadable
from knowledge_base.text_limits import CHUNK_TARGET_CHARS

UNREADABLE_MESSAGE = "This Word 97–2003 file could not be read; save it as .docx or PDF"
ENCRYPTED_MESSAGE = (
    "This Word document is password protected; remove the password and "
    "upload it again."
)

_WORD_IDENT = 0xA5EC
# Word 97 and later; older (Word 6 / 95) files have a different FIB.
_MIN_NFIB = 0xC1
_FLAG_ENCRYPTED = 0x0100
_FLAG_WHICH_TABLE = 0x0200
_FLAG_OBFUSCATED = 0x8000
_FIB_BASE_SIZE = 32
# Index of ccpText in FibRgLw97 and of the fcClx/lcbClx pair in FibRgFcLcb97.
_CCP_TEXT_INDEX = 3
_CLX_PAIR_INDEX = 33
_CLXT_PRC = 0x01
_CLXT_PCDT = 0x02
_PCD_SIZE = 8
_FC_COMPRESSED = 0x40000000
_FC_MASK = 0x3FFFFFFF

_FIELD_BEGIN = "\x13"
_FIELD_SEPARATOR = "\x14"
_FIELD_END = "\x15"
_PARAGRAPH_MARKS = frozenset("\r\x07")  # paragraph end, table cell / row end
_LINE_BREAKS = frozenset("\x0b\x0c\x0e")  # line, page and column breaks
_NON_BREAKING_HYPHEN = "\x1e"
_OLE_ERRORS = (OSError, ValueError, IndexError, KeyError, struct.error)
# A piece is decoded this many characters at a time.
_DECODE_BLOCK_CHARS = 65536
# Text read past the item budget, so stripped paragraphs still fill it.
_TEXT_MARGIN_CHARS = 4096


def extract_word97(data: bytes, collector: SectionCollector) -> None:
    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            word = _read_stream(ole, "WordDocument")
            flags, table_name, ccp_text, fc_clx, lcb_clx = _read_fib(word)
            if flags & (_FLAG_ENCRYPTED | _FLAG_OBFUSCATED):
                raise EncryptedDocument(ENCRYPTED_MESSAGE)
            table = _read_stream(ole, table_name)
    except _OLE_ERRORS as exc:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE) from exc
    pieces = _read_piece_table(table, fc_clx, lcb_clx)
    _check_text_size(len(word), pieces, ccp_text)
    text, cut = _main_text(word, pieces, ccp_text, collector.room + _TEXT_MARGIN_CHARS)
    if cut:
        collector.truncated = True
    _add_paragraphs(text, collector)


def _read_stream(ole: olefile.OleFileIO[str], name: str) -> bytes:
    if not ole.exists(name):
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    return ole.openstream(name).read()


def _read_fib(word: bytes) -> tuple[int, str, int, int, int]:
    """(flags, table stream name, ccpText, fcClx, lcbClx)."""
    ident, nfib = _unpack("<HH", word, 0)
    if ident != _WORD_IDENT or nfib < _MIN_NFIB:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    (flags,) = _unpack("<H", word, 0x0A)
    offset = _FIB_BASE_SIZE
    (csw,) = _unpack("<H", word, offset)
    offset += 2 + csw * 2
    (cslw,) = _unpack("<H", word, offset)
    if cslw <= _CCP_TEXT_INDEX:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    (ccp_text,) = _unpack("<i", word, offset + 2 + _CCP_TEXT_INDEX * 4)
    offset += 2 + cslw * 4
    (pair_count,) = _unpack("<H", word, offset)
    if pair_count <= _CLX_PAIR_INDEX:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    fc_clx, lcb_clx = _unpack("<II", word, offset + 2 + _CLX_PAIR_INDEX * 8)
    table_name = "1Table" if flags & _FLAG_WHICH_TABLE else "0Table"
    if ccp_text < 0:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    return flags, table_name, ccp_text, fc_clx, lcb_clx


def _read_piece_table(
    table: bytes, fc_clx: int, lcb_clx: int
) -> list[tuple[int, int, int, bool]]:
    """Pieces as (cp start, cp end, byte offset in WordDocument, compressed)."""
    end = fc_clx + lcb_clx
    if lcb_clx == 0 or end > len(table):
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    position = fc_clx
    # Skip the Prc entries (formatting) until the Pcdt; each step advances.
    while position < end and table[position] == _CLXT_PRC:
        (size,) = _unpack("<h", table, position + 1)
        if size < 0:
            raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
        position += 3 + size
    if position >= end or table[position] != _CLXT_PCDT:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    (plc_size,) = _unpack("<I", table, position + 1)
    plc_start = position + 5
    if plc_start + plc_size > end or plc_size < 4 or (plc_size - 4) % 12:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    count = (plc_size - 4) // 12
    cps = struct.unpack_from(f"<{count + 1}I", table, plc_start)
    descriptors = plc_start + (count + 1) * 4
    pieces: list[tuple[int, int, int, bool]] = []
    for index in range(count):
        (fc_value,) = _unpack("<I", table, descriptors + index * _PCD_SIZE + 2)
        compressed = bool(fc_value & _FC_COMPRESSED)
        fc = fc_value & _FC_MASK
        offset = fc // 2 if compressed else fc
        if cps[index + 1] < cps[index]:
            raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
        pieces.append((cps[index], cps[index + 1], offset, compressed))
    return pieces


def _check_text_size(
    word_size: int, pieces: list[tuple[int, int, int, bool]], ccp_text: int
) -> None:
    """Refuse text the stream cannot hold: at least one byte per character."""
    if ccp_text > word_size:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    total = 0
    for cp_start, cp_end, offset, compressed in pieces:
        if cp_start >= ccp_text:
            break
        chars = min(cp_end, ccp_text) - cp_start
        size = chars if compressed else chars * 2
        total += size
        # Pieces of a real file never overlap, so together they fit too.
        if offset + size > word_size or total > word_size:
            raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)


def _main_text(
    word: bytes,
    pieces: list[tuple[int, int, int, bool]],
    ccp_text: int,
    limit: int,
) -> tuple[str, bool]:
    """The main text without field codes, at most ``limit`` characters.

    The flag is True when the text was cut at ``limit``.
    """
    fields = _FieldFilter()
    for cp_start, cp_end, offset, compressed in pieces:
        if cp_start >= ccp_text:
            break
        width = 1 if compressed else 2
        end = offset + (min(cp_end, ccp_text) - cp_start) * width
        step = _DECODE_BLOCK_CHARS * width
        decoder = codecs.getincrementaldecoder("cp1252" if compressed else "utf-16-le")(
            errors="replace"
        )
        for block in range(offset, end, step):
            block_end = min(block + step, end)
            fields.feed(decoder.decode(word[block:block_end], block_end == end))
            if fields.length >= limit:
                return fields.text()[:limit], True
    return fields.text(), False


# Real documents nest fields a few levels deep.
_MAX_FIELD_DEPTH = 64


class _FieldFilter:
    """Keeps field results, drops field codes and control characters."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.length = 0
        # Per open field: True while its code part (before the separator) runs.
        # At most _MAX_FIELD_DEPTH entries; deeper fields are counted in
        # _deep_codes and stay in their code part (a crafted file cannot grow
        # this list by one entry per byte).
        self._fields: list[bool] = []
        self._deep_codes = 0
        # How many open fields are in their code part.
        self._open_codes = 0

    def feed(self, text: str) -> None:
        output: list[str] = []
        for char in text:
            if char == _FIELD_BEGIN:
                if len(self._fields) < _MAX_FIELD_DEPTH:
                    self._fields.append(True)
                else:
                    self._deep_codes += 1
                self._open_codes += 1
            elif char == _FIELD_SEPARATOR:
                if self._deep_codes:
                    continue
                if self._fields and self._fields[-1]:
                    self._fields[-1] = False
                    self._open_codes -= 1
            elif char == _FIELD_END:
                if self._deep_codes:
                    self._deep_codes -= 1
                    self._open_codes -= 1
                elif self._fields and self._fields.pop():
                    self._open_codes -= 1
            elif self._open_codes:
                continue
            elif char in _PARAGRAPH_MARKS:
                output.append("\n")
            elif char in _LINE_BREAKS:
                output.append("\n")
            elif char == _NON_BREAKING_HYPHEN:
                output.append("-")
            elif char == "\t" or ord(char) >= 32:
                output.append(char)
        block = "".join(output)
        self._parts.append(block)
        self.length += len(block)

    def text(self) -> str:
        return "".join(self._parts)


def _add_paragraphs(text: str, collector: SectionCollector) -> None:
    # (1-based offset of the paragraph's first character, paragraph text)
    paragraphs: list[tuple[int, str]] = []
    offset = 1
    for line in text.split("\n"):
        if line.strip():
            paragraphs.append((offset, line.strip()))
        offset += len(line) + 1
    sizes = [len(paragraph) + 1 for _, paragraph in paragraphs]
    for start, end in group_by_size(sizes, CHUNK_TARGET_CHARS):
        first = paragraphs[start][0]
        last_offset, last_text = paragraphs[end - 1]
        location = range_label(
            "Word text", "Word text", first, last_offset + len(last_text) - 1
        )
        body = "\n".join(paragraph for _, paragraph in paragraphs[start:end])
        if not collector.add(location, "", body):
            return


def _unpack(fmt: str, buffer: bytes, offset: int) -> tuple[int, ...]:
    if offset < 0 or offset + struct.calcsize(fmt) > len(buffer):
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
    return struct.unpack_from(fmt, buffer, offset)
