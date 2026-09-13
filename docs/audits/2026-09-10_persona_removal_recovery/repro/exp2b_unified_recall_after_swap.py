"""実験 2b: memory.db を差し替えた B で、自動想起 (unified_recall) が
A の記憶を拾うか。

exp2 が作った exp2_home をそのまま使う (差し替え済みの B)。
`sai_memory.unified_recall.unified_recall` は sea/auto_recall.py:867 が呼ぶ
本番の自動想起の芯。conn は B の adapter が開いた memory.db。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_env import guard_not_production, setup_home  # noqa: E402

HOME = setup_home("exp2")   # exp2 の成果物 (差し替え済み) を再利用
guard_not_production()

from saiverse_memory.adapter import SAIMemoryAdapter  # noqa: E402
from sai_memory.unified_recall import unified_recall  # noqa: E402

B = "probe_b_testcity"
OUT: dict = {}

b = SAIMemoryAdapter(
    persona_id=B, persona_dir=HOME / "personas" / B,
    resource_id=B, startup_backup=False, recover_orphaned_thread=False,
)
hits = unified_recall(
    b.conn, b.embedder, "棚と定規の話",
    search_messages=True, persona_id=B,
)
OUT["unified_recall"] = [
    {
        "source_type": h.source_type,
        "uri": h.uri,
        "title": (h.title or "")[:50],
        "content": (h.content or "")[:70],
        "score": round(float(h.score), 4) if h.score is not None else None,
    }
    for h in hits
]
OUT["hit_count"] = len(hits)
b.close()

out = Path(__file__).with_name("exp2b_result.json")
out.write_text(json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(OUT, ensure_ascii=False, indent=2))
