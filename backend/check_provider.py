from __future__ import annotations

import asyncio
import json

from backend.app.config import Settings
from backend.app.llm import configured_provider


async def main() -> int:
    settings = Settings()
    provider = configured_provider(settings)
    status = await provider.healthcheck()
    print(
        json.dumps(
            {
                "status": status.get("status"),
                "provider": status.get("provider"),
                "model": status.get("model"),
                "fallback": status.get("fallback"),
                "error_code": status.get("error_code"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if status.get("status") == "available" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
