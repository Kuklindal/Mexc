import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("mexc_p2p")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    logger.addHandler(console)
    if os.getenv("LOG_TO_FILE", "true").strip().lower() not in {"0", "false", "no", "off"}:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(log_dir) / "mexc_p2p.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger
