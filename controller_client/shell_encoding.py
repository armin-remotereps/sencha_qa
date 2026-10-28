from __future__ import annotations

import sys
from typing import Final

# Encoding of shell command output. On Windows cmd built-ins write the OEM code
# page to a pipe; decode with errors="replace" so stray bytes never raise.
if sys.platform == "win32":
    SHELL_OUTPUT_ENCODING: Final[str] = "oem"
else:
    SHELL_OUTPUT_ENCODING = "utf-8"
