from __future__ import annotations

import os

import uvicorn

from config.settings import Settings


def run() -> None:
    os.environ["COMPARE_READ_ONLY"] = "1"
    settings = Settings.load()
    uvicorn.run(
        "systems.compare.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    run()

