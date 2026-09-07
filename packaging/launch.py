"""Entry point for the packaged app.

Two things differ from `agentfeed desktop` on a dev machine: the UI and
domain packs live inside the bundle, and a windowed app has no terminal to
print to, so logs go to the data directory instead.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def main() -> None:
    root = _bundle_root()
    os.environ.setdefault("AGENTFEED_UI_DIR", str(root / "ui"))
    os.environ.setdefault("AGENTFEED_PACKS_DIR", str(root / "agentfeed" / "domains"))

    from agentfeed.config import settings
    settings.ensure_dirs()
    logging.basicConfig(
        filename=str(Path(settings.data_dir) / "agentfeed.log"),
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from agentfeed.desktop import run
    run()


if __name__ == "__main__":
    main()
