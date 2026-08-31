# Issue: 構造化出力 (dict) の memorize が SQLite bind で黙って落ちる

**ステータス**: ✅ 解決済み (2026-07-19)
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

## ログ

- 2026-07-19: **解決 (修正方向 1 = 書ける口の正規化)**。真因を実コードで確定: メッセージ挿入は `saiverse_memory/adapter.py:_append_message` が唯一の関所 (`add_message(` 呼び出しはここ 1 箇所のみ、public な `append_building_message` / `append_persona_message` / `append_ledger_message` は全てここへ集約)。`content = message.get("content", "")` を str 化せず `add_message(content=...)` の TEXT 列 bind に渡していたため、dict が来ると `type 'dict' is not supported` で例外 → 同関数末尾の `except` が WARN 握り潰し → `None` を返して memorize が消えていた。修正: モジュール関数 `_coerce_content_to_text` を新設し content 抽出直後で正規化 — str はそのまま / dict・list は `json.dumps(ensure_ascii=False)` で JSON 化 (可読・往復可能) / None は "" / その他は `str()`。関所 1 箇所なので全 append 経路 (chat・pulse・memorize) を一括でカバー。`update_message` の raw `UPDATE ... content=?` 経路は訂正用で呼び出しが str のため今回スコープ外。回帰 `tests/test_memorize_dict_content.py` 追加 (純関数 5 ケース + 実 DB 統合 2 ケース = dict が JSON で永続化され mid が返る / str は verbatim)。adapter・memory 系スイート 53 passed。
- **副次メモ**: whole-dict が JSON 文字列としてそのまま記憶本文に載るのは「握り潰しゼロ」の最低保証であって、ペルソナの記憶本文としての体裁は playbook 側で `{key.leaf}` を参照する (= structured output の意図された使い方、`lg_memorize_node` は dict を flatten してドット記法で参照可能にしている) のが本筋。JSON 生載りが気になる playbook が出たらそちらを直す (本 issue のスコープ外)。
