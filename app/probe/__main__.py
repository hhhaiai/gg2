"""Entry point — `python -m app.probe`.

Boots a minimal runtime: config, logging, repository.  No HTTP server, no
admin endpoints, no leader election.  Just the probe loop.
"""

from __future__ import annotations

import asyncio
import os
import signal

from app.control.account.backends.factory import create_repository
from app.platform.config.snapshot import config as _config
from app.platform.logging.logger import logger, reload_logging, setup_logging
from .runner import ProbeRunner


async def _main() -> int:
    setup_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        file_logging=os.getenv("LOG_FILE_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"},
    )
    await _config.load()
    reload_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        file_level=_config.get_str("logging.file_level", "") or None,
        max_files=_config.get_int("logging.max_files", 7),
    )
    logger.info("probe process starting: pid={}", os.getpid())

    repo = create_repository()
    await repo.initialize()
    runner = ProbeRunner(repo)

    loop = asyncio.get_running_loop()
    stop_signals = (signal.SIGINT, signal.SIGTERM) if hasattr(signal, "SIGTERM") else (signal.SIGINT,)
    for sig in stop_signals:
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(runner)))
        except NotImplementedError:
            # Windows / restricted environments — fall back to default handler.
            pass

    try:
        await runner.run_forever()
    finally:
        await runner.shutdown()
        await repo.close()
        logger.info("probe process stopped")
    return 0


async def _shutdown(runner: ProbeRunner) -> None:
    logger.info("probe received shutdown signal")
    await runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
