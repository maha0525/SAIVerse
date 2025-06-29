import asyncio
import os
from typing import Optional

import cognee

async def build_knowledge_graph(log_file: str, dataset_name: str = "chat_history") -> None:
    """ログファイルを読み込み Knowledge Graph を構築して保存します。"""
    if not os.path.exists(log_file):
        raise FileNotFoundError(log_file)
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("USER: "):
                await cognee.add(line[6:], dataset_name=dataset_name)
            elif line.startswith("BOT: "):
                await cognee.add(line[5:], dataset_name=dataset_name)
    await cognee.cognify(dataset_name)


def main(path: Optional[str] = None) -> None:
    log_path = path or os.path.join("logs", "sophie.log")
    asyncio.run(build_knowledge_graph(log_path))


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else None)