"""Word 97–2003 (.doc) main text through the MS-DOC piece table (best effort).

1. ``olefile`` opens the file and reads the ``WordDocument`` stream's FIB.
2. A set ``fEncrypted`` (or ``fObfuscated``) is refused.
3. ``fWhichTblStm`` selects ``0Table`` / ``1Table``, where the CLX and its
   PlcPcd piece table are read (``fcClx`` / ``lcbClx`` of FibRgFcLcb97).
4. Each piece of the main text (CPs ``0 .. ccpText``) is decoded as cp1252
   (compressed) or UTF-16LE.
5. Field codes and control characters are dropped.

Every offset is bounds-checked and every loop is bounded by the stream
sizes; anything unexpected raises ``LegacyFormatUnreadable``. Sections are
cut at paragraph marks with character-offset locations ("Word text
12 001–13 500"); the format has no page numbers without layout.
"""

from __future__ import annotations

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
    text = _main_text(word, pieces, ccp_text)
    _add_paragraphs(_drop_fields(text), collector)


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


def _main_text(
    word: bytes, pieces: list[tuple[int, int, int, bool]], ccp_text: int
) -> str:
    parts: list[str] = []
    for cp_start, cp_end, offset, compressed in pieces:
        if cp_start >= ccp_text:
            break
        chars = min(cp_end, ccp_text) - cp_start
        size = chars if compressed else chars * 2
        if offset + size > len(word):
            raise LegacyFormatUnreadable(UNREADABLE_MESSAGE)
        raw = word[offset : offset + size]
        encoding = "cp1252" if compressed else "utf-16-le"
        parts.append(raw.decode(encoding, errors="replace"))
    return "".join(parts)


def _drop_fields(text: str) -> str:
    """Keep field results, drop field codes and control characters."""
    output: list[str] = []
    # Per open field: True while its code part (before the separator) runs.
    fields: list[bool] = []
    for char in text:
        if char == _FIELD_BEGIN:
            fields.append(True)
        elif char == _FIELD_SEPARATOR:
            if fields:
                fields[-1] = False
        elif char == _FIELD_END:
            if fields:
                fields.pop()
        elif any(fields):
            continue
        elif char in _PARAGRAPH_MARKS:
            output.append("\n")
        elif char in _LINE_BREAKS:
            output.append("\n")
        elif char == _NON_BREAKING_HYPHEN:
            output.append("-")
        elif char == "\t" or ord(char) >= 32:
            output.append(char)
    return "".join(output)


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
