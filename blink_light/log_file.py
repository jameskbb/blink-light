"""The one log file the scheduler loop and the watcher both append to."""

from __future__ import annotations

import logging
from pathlib import Path

from .state import rotate_log


def open_log(logger: logging.Logger, path: Path, tag: str) -> logging.Handler:
    """Point ``logger`` at the shared log, moving an oversized one aside first.

    Both processes must write the same encoding. The watcher used to go through
    ``basicConfig``, which writes in the Windows code page, so the file mixed
    two encodings and a line with a character the code page lacks - an emoji
    in an override reason, a localised error - was dropped, with the complaint
    sent to a stderr nobody reads. The tag says which process wrote each line.

    Returns the handler; call ``close_log`` when done; a handler left attached
    holds the file open and stacks up if the loop runs twice in one process.
    """
    close_log(logger)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(path)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{tag}] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def close_log(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
