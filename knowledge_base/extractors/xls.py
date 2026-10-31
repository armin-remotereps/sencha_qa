"""Excel 97–2003 (.xls) sheets with xlrd, rows tab-separated.

Same locations and shared cell cap as the .xlsx reader; dates are converted
with ``xldate_as_datetime``.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

import xlrd
from xlrd.biffh import XLRDError
from xlrd.book import Book
from xlrd.sheet import Cell, Sheet
from xlrd.xldate import XLDateError, xldate_as_datetime

from knowledge_base.chunking import SectionCollector
from knowledge_base.exceptions import EncryptedDocument, LegacyFormatUnreadable
from knowledge_base.extractors.xlsx import (
    CellBudget,
    SheetRow,
    add_sheet_rows,
    format_cell,
)

UNREADABLE_MESSAGE = (
    "This Excel 97–2003 file could not be read; save it as .xlsx or CSV."
)
ENCRYPTED_MESSAGE = (
    "This Excel file is password protected; remove the password and upload it again."
)
_READ_ERRORS = (
    XLRDError,
    struct.error,
    IndexError,
    KeyError,
    ValueError,
    AssertionError,
)


def extract_xls(data: bytes, collector: SectionCollector) -> None:
    try:
        book = xlrd.open_workbook(
            file_contents=data, on_demand=True, formatting_info=False
        )
    except XLRDError as exc:
        if "encrypted" in str(exc).lower():
            raise EncryptedDocument(ENCRYPTED_MESSAGE) from exc
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE) from exc
    except _READ_ERRORS as exc:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE) from exc
    try:
        cells = CellBudget()
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            more = add_sheet_rows(sheet.name, _rows(book, sheet), cells, collector)
            book.unload_sheet(index)
            if not more:
                return
    except _READ_ERRORS as exc:
        raise LegacyFormatUnreadable(UNREADABLE_MESSAGE) from exc
    finally:
        book.release_resources()


def _rows(book: Book, sheet: Sheet) -> Iterable[SheetRow]:
    for index in range(sheet.nrows):
        row = sheet.row(index)
        yield index + 1, len(row), (_cell_text(book, cell) for cell in row)


def _cell_text(book: Book, cell: Cell) -> str:
    if cell.ctype == xlrd.XL_CELL_DATE and isinstance(cell.value, float):
        try:
            return format_cell(xldate_as_datetime(cell.value, book.datemode))
        except (XLDateError, OverflowError):
            return format_cell(cell.value)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return format_cell(bool(cell.value))
    if cell.ctype == xlrd.XL_CELL_ERROR and isinstance(cell.value, int):
        return xlrd.error_text_from_code.get(cell.value, "#ERROR")
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    return format_cell(cell.value)
