# Memopedia カテゴリ語彙のハードコード散在（追加時の追従漏れが構造的に起きる）

**起票**: 2026-07-11（P3c① テーマカテゴリ追加の実機検証で UI 非表示バグが発覚したのを受け、まはー指示）
**種別**: アーキテクチャ負債（prep-refactor 候補）
**関連**: `docs/intent/concept_consolidation.md` P3c① 実機検証での追修正 / Memory Atlas category 定義表

## 問題

Memopedia のカテゴリ語彙（people / terms / plans / events / theme）が**単一の真実を持たず**、Python・TypeScript・LLM プロンプト文字列・JSON スキーマ・CLI 引数の5形態で全域にハードコードされている。カテゴリを1つ増やすたびに十数箇所の列挙を手で追従する必要があり、漏れても**エラーにならず黙って落ちる**:

- `build_tree` は列挙外カテゴリの trunk をツリー構築時点で捨てる（例外なし・ログなし）
- ツール引数の `enum` は新カテゴリへの書き込みを静かに拒む
- メンテナンスのループは列挙外カテゴリを対象にしないまま「全件処理した」顔をする

## これは仮説ではない — 既に3種類のドリフトが実在する

| ドリフト | 実態 | 状態 |
|---|---|---|
| **terms 漏れ** | `scripts/maintain_memopedia.py` の全5ループが `["people", "events", "plans"]` — **用語ページは merge-similar / split-large 等のメンテを一度も受けていない** | 未修正 |
| **events 漏れ** | `memopedia_save_page` / `memopedia_note` の enum・生成 API のバリデーション（`api/routes/people/memopedia.py` の「Must be 'people', 'terms', or 'plans'」）・`sai_memory/memopedia/generator.py` のプロンプト・`scripts/organize_to_trunk.py`・`scripts/memopedia/build_memopedia_core.py` が people/terms/plans 止まり — events への手動書き込み・生成経路が閉じている（意図か漏れか判定不能。**意図を記録する場所が無いことこそが本 issue**） | 未修正・要判定 |
| **theme 漏れ** | P3c① で移行は成功したのに `build_tree` / `get_tree` の固定列挙で UI に一切表示されず（2026-07-11 実機検証で発覚 → コミット 5923458 で追修正） | 修正済 |

3つが**それぞれ違うサブセット**で腐っている＝コピーの数だけ独立に劣化する、という散在の教科書的症状。

## 箇所台帳（2026-07-11 時点。行番号は目安、再監査は末尾の grep で）

### A. 構造層（ツリー構築・表示・エクスポート）
- `sai_memory/memopedia/storage.py` — `CATEGORY_*` 定数（現状唯一「定義」と呼べる場所）/ `INITIAL_ROOTS` / `build_tree` の固定 result dict（**列挙外カテゴリを黙って捨てる本丸**）
- `sai_memory/memopedia/core.py` — `get_tree` の返却 dict / `get_tree_markdown` の category_names + ループ / `export_all_markdown` の category_names + ループ / 検索系 docstring
- `sai_memory/theme_pages.py` — `CATEGORY_THEME` は storage から import（P3c① 追修正で一元化済み・この形が原型）

### B. スペル / ツール層
- `builtin_data/tools/memory_write.py` — enum（people/terms/plans/events。theme は意図的除外）
- `builtin_data/tools/memopedia_save_page.py` / `memopedia_note.py` — enum（people/terms/plans。events 漏れ）
- `builtin_data/tools/memopedia_health.py` — 走査タプル
- `builtin_data/tools/get_memory_weave_context.py` — category_names + ループ（自動想起の索引）

### C. 編纂パイプライン（LLM プロンプト内の列挙を含む）
- `sai_memory/memory/entity_extractor.py` — `VALID_CATEGORIES` set / プロンプト文字列 / `_format_page_list` のタプル
- `sai_memory/memopedia/generator.py` — プロンプトのカテゴリヒント / 出力 JSON スキーマ文字列
- `sai_memory/memory/note_organizer.py` — ループ / プロンプト / category_names
- `scripts/maintain_memopedia.py` — 5ループ（terms 漏れの現場）
- `scripts/organize_to_trunk.py` — CLI choices / dict / category_names / 全カテゴリリスト / プロンプト
- `scripts/memopedia/build_memopedia_core.py` — root 対応 dict / スキーマ enum

### D. API / フロントエンド
- `api/routes/people/memopedia.py` — 生成エンドポイントのバリデーション（events 漏れ）
- `api/routes/people/models.py` — カテゴリコメント
- `frontend/src/components/memory/MemopediaViewer.tsx` — `TreeStructure` interface / 集約スプレッド6箇所 / sortedTree / flatPages collect / カテゴリ節の描画 / 生成モーダルの option

## 直し方の指針（単一リスト化では**ない**）

カテゴリには**役割別の可視性**があり、全消費者が同じリストを見ればよいわけではない:

- theme は「本人が立てる」地図 — entity_extractor / generator / 生成 UI の**書き先にしてはいけない**（P3c① で意図的に除外、`storage.py` のコメントに明記済み）
- core / chronicle は build_tree に**出さない**のが正しい（コア記憶は head 常設、Chronicle は別導線）

したがって正しい形は **storage.py にカテゴリレジストリを一元化**し、各箇所は役割で問い合わせる:

```python
CATEGORY_DEFS = {
    "people": {"label": "人物", "in_tree": True, "extractable": True, "writable": True},
    "terms":  {"label": "用語", "in_tree": True, "extractable": True, "writable": True},
    "plans":  {"label": "予定", "in_tree": True, "extractable": True, "writable": True},
    "events": {"label": "出来事", "in_tree": True, "extractable": True, "writable": <要判定>},
    "theme":  {"label": "テーマ", "in_tree": True, "extractable": False, "writable": False},
    # core / chronicle は in_tree=False の登録カテゴリとして明示する
    #（「登録済みだが非表示」と「未登録＝バグ」を区別できるようにする）
}
```

- ループ・enum・category_names は全てここから導出（`categories(role="extractable")` 等）
- LLM プロンプト内の列挙もレジストリから組み立てる（文字列にベタ書きしない）
- フロントは API から受ける（get_tree がキー+ラベルを返す。TS 側のハードコードを撤去）
- **防御**: `build_tree` がレジストリ外カテゴリのページを捨てるとき WARN ログを出す（silent drop の検出。これだけなら即日できる安価な先行手当）

## 発火条件

- **次にカテゴリを増やす前**（確定候補あり: 第四の地図「世界」構想、P4 代謝で theme の扱いが動く時）— prep-refactor として先にやる
- または terms 漏れ（メンテ対象外）/ events 漏れ（書き込み経路）の実害を直すついで

## 修正時の検証

本台帳の再監査 grep（残存ゼロを確認する）:

```bash
grep -rn "\"people\".*\"terms\"\|'people'.*'terms'\|people.*terms.*plans" --include=*.py .
grep -rn "people.*events.*plans" --include=*.py scripts/
grep -rn "tree.people\|tree\.terms" frontend/src/
```

---

## 解決（2026-07-11、P4-0 カテゴリレジストリ）

`CATEGORY_DEFS`（storage.py、役割フラグ: in_tree / hide_when_empty / extractable / writable / metabolizable）に一元化し、台帳 A〜D の全列挙を役割導出に置換。build_tree にレジストリ外カテゴリの WARN。フロントはツリー API の categories メタで動的化。**実装中に第4・第5のドリフトも発見・修正**: extraction プロンプトに events が無い／system 抽出プロンプトに terms が無い（プロンプト文字列の列挙は台帳の想定以上に腐っていた）。「予定」「計画」のラベル表記揺れも「計画」に統一（既存 DB の root_plans trunk タイトルは既定名の場合のみ冪等リネーム）。再監査 grep の残存はレジストリ本体・例示・無関係の "people" ルータタグ, いずれも良性。コミット bf983a0 ほか。
