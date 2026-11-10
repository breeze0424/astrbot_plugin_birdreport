#!/usr/bin/env python3
"""离线重建城市列表：重新抓取 birdreport 省市数据，更新 data/regions.json 与 _conf_schema.json。

用法（在插件目录下执行）：
    python rebuild_regions.py

等价于插件内的 `/刷新城市` 指令，只是不需要启动 AstrBot。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.client import BirdReportClient  # noqa: E402
from core import regions as regions_mod  # noqa: E402
from core import schema as schema_mod  # noqa: E402


async def main() -> int:
    client = BirdReportClient(timeout=25)
    try:
        regions = await regions_mod.build_regions(client)
        schema_mod.write_schema([r["name"] for r in regions])
    finally:
        await client.close()
    print(f"已更新：{len(regions)} 个城市 → data/regions.json 与 _conf_schema.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
