from __future__ import annotations

import uvicorn

from config.settings import Settings


def run() -> None:
    settings = Settings.load()
    uvicorn.run(
        "systems.youpin.app:app",
        host=settings.app_host,
        port=8102,
        reload=False,
    )


if __name__ == "__main__":
    run()

