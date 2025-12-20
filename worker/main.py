from __future__ import annotations

import logging
import time
from pathlib import Path

from app.config import load_config
from storage.db import Database

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("worker")


def main() -> None:
    config = load_config(Path("config.yaml") if Path("config.yaml").exists() else None)
    db = Database(config.db_path)
    db.initialize()
    logger.info("Worker started with music_root=%s", config.music_root)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")


if __name__ == "__main__":
    main()
