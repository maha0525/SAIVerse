# Issue: 構造化出力 (dict) の memorize が SQLite bind で黙って落ちる

**ステータス**: 🔵 未解決 (修正待ち — 小物)
**作成日**: 2026-07-19
**関連**: `sea/runtime.py` `_store_memory` / `saiverse_memory/adapter.py` `_append_message`

## 実機観測 (2026-07-18 00:00:15, sophie_city_a)

スペル引数決定ノード (`Spell 引数決定` playbook、structured output) が dict を返し、node に `memorize=True` が付いていたため `_store_memory` が dict のまま adapter へ渡り:

```
[WARNING] saiverse_memory.adapter: Failed to append message to SAIMemory (building=None):
Error binding parameter 4: type 'dict' is not supported
```

→ **memorize 指定が黙って失われた** (WARN 止まりで処理続行)。当該メッセージは sub_line/volatile だったため実害は小さいが、経路としては committed でも同じことが起きる。

## 問題の構造

- LLM node の結果が structured output (dict) の場合、`content` が str でないまま `messages.content` (TEXT 列) に bind される
- adapter 側は例外を WARN で握って続行 — 「memorize しろ」という設計指示が果たされないのに上流へは表明されない ([[feedback_debugging_discipline]] のエラー握り潰し類型)

## 修正方向

1. `_store_memory` (または adapter `_append_message` 入口) で content が str でなければ JSON 文字列化して保存 (`json.dumps(ensure_ascii=False)`) — memorize の意図 (この結果を記憶に残す) を素直に果たす
2. adapter の bind 失敗 WARN はそのまま残してよいが、型不一致はそこに至る前に潰す (書ける口の正規化)
3. 回帰: dict / list / str の各 content で append が成功し、dict は JSON 文字列として往復すること

修正は小さい (関所 1 箇所 + テスト)。experience_structure と独立、いつでも着手可。
