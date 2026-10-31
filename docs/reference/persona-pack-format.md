# ペルソナまるごとパックの形式

本体と SAIVerse Lite が受け渡す ZIP の形式の仕様。なぜこの形なのかは [persona_pack.md](../intent/persona_pack.md)、Lite との互換の約束は [saiverse_lite.md](../intent/saiverse_lite.md) §5 にある。どちらかが形式を変えるときは、同じ変更の中で両側を更新する。

実装: 書き出しは `saiverse_memory/persona_pack/export.py`、取り込みは `importer.py`、値の変換は `format.py`、`extensions/lite/changes.json` は `return_merge.py`。

## ZIP の中身

`manifest.json` の `format` は `persona-pack`、`version` は `1.x`。`contents` に同梱フォルダ名を列挙し、取り込みは `contents` にあるフォルダだけを読む。全項目の割り当ての確定表は intent §9。

| フォルダ | 中身 |
|---|---|
| `manifest.json` | `format`・`version`・`persona_id`・`persona_name`・`contents` など |
| `profile/profile.json` | `ai`（ai 行の全列）と `files`（アイコンと外見画像の ZIP 内パス） |
| `chatlog/` | `threads.json`・`messages.json`・`stelis_threads.json` |
| `knowledge/pages.json` | Memopedia の全ページ（コア記憶と Chronicle を含む） |
| `attachments/` | 添付の実体と `index.json` |
| `journal/` | `activities.json`・`memos.json`・`clips.json`・`purpose_tags.json`・`task_book.json`・`day_reports/` |
| `items/` | `private_room.json`・`items.json` とファイル |
| `extensions/saiverse/` | `memory_db/<表名>.json`・`saiverse_db/<表名>.json`・`persona_dir/`（`active_state.json` など） |
| `extensions/lite/changes.json` | Lite が書き出すパックだけが持つ（下の節） |
| `chatgpt_compat/` | 他アプリ向けの写し。本体の取り込みは読まない |

値の決まりは intent §3 原則 5: バイナリは `{"__bytes_b64__": ...}`、日時はタイムゾーン付き ISO 8601、JSON 文字列の列は展開して書く。

## `extensions/lite/changes.json`

Lite で変えた記録の一覧。このファイルがあるパックの合流は、intent §5「Lite から帰るときの合流」に従う。`manifest.json` の `contents` に `extensions/lite` を入れる。ファイルと `contents` が食い違うパックは断る。

```json
{
  "format": "saiverse-lite-changes",
  "version": 1,
  "messages": { "created": [], "edited": [], "deleted": [] },
  "pages": { "created": [], "edited": [], "deleted": [] },
  "fragments": { "created": [] },
  "profile": { "edited": [] }
}
```

六つの欄のどれが欠けてもパック全体を断る。空の配列は書く。

**配列の要素**は id の文字列か、`{"id": "...", "changed_at": ...}` のオブジェクト。`changed_at` は Lite で変えた時刻（UNIX 秒か ISO 8601）で、見比べの画面に更新日時として出す。発言の行は更新日時を持たないので、編集と削除には付けることを勧める。付けないと、編集は発言の `created_at`、削除は空で出る。

**id の条件**:

- `messages` と `pages` の id は本体の id のまま。Lite で生まれた行は新しい id を振る。
- 同じ欄の `created`・`edited`・`deleted` に同じ id を二度書かない。Lite で作って編集した行は `created` だけ、Lite で作って消した行はどこにも書かない。
- `created` と `edited` の id の行は、`chatlog/messages.json` か `knowledge/pages.json` に必ず入れる。無ければ断る。
- `pages.edited` の行は ZIP で `is_deleted` にしない。消した行は `deleted` に書く。
- `deleted` の id は ZIP に無くてよい。ZIP にあっても足さない。
- `fragments.created` の id の行は `extensions/saiverse/memory_db/memopedia_fragments.json` に入れる。
- `profile.edited` の要素は `AINAME`・`SYSTEMPROMPT`・`DESCRIPTION`、または同じ意味の `name`・`system_prompt`・`description`。値は `profile/profile.json` の `ai` に入れる。
- Lite で生まれたスレッドの id は `<persona_id>:<suffix>` の形にする。
- Chronicle と根のページ（`root_` で始まるもの）は `pages` に書いても一覧に出さない。結果の注意に載る。

**いま続いているスレッド**は `changes.json` に書かない。`extensions/saiverse/persona_dir/active_state.json` の `active_thread_id` に、スレッド id の `:` より後ろを書く。

合流で足すのは、`created` の行と、利用者が戻すと選んだ行だけ。`journal/` の行は足さない。ZIP の `chatlog/` と `knowledge/` に一覧に無い行があっても、本体の値を変えない。
