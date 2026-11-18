"""Small filesystem helpers shared by the writers of user-visible files."""
from __future__ import annotations

import os
import threading
import time
from typing import Union

from antra.utils.longpath import extended_path


def write_atomic(
    path: str,
    data: Union[str, bytes],
    *,
    encoding: str = "utf-8",
    newline: "str | None" = None,
) -> None:
    """Write `data` to `path` so a failure never leaves a truncated file.

    The content goes to a sibling temp file first and is moved over the target
    with os.replace, so an encoding error, a full disk or a kill mid-write
    leaves the PREVIOUS file exactly as it was (a truncating open() had already
    emptied it by then). The temp file is removed on any failure. Raises
    whatever the write raised, including UnicodeEncodeError.
    """
    real = extended_path(path)
    tmp = f"{real}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        if isinstance(data, str):
            with open(tmp, "w", encoding=encoding, newline=newline) as fh:
                fh.write(data)
        else:
            with open(tmp, "wb") as fh:
                fh.write(data)
        # Windows: a scanner or indexer holding the target briefly blocks the rename.
        for attempt in range(5):
            try:
                os.replace(tmp, real)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
