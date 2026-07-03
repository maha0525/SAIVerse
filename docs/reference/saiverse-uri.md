# `saiverse://` URI スキーム

SAIVerse 内のリソース（過去ログ・記憶・アイテム・画像・Web 等）を統一アドレスで参照する仕組み。ペルソナは Playbook / Spell の中でこの URI を使い、`memory_recall` や画像生成の参照画像などに渡す。

- 実装: [`saiverse/uri_resolver.py`](../../saiverse/uri_resolver.py)（`UriResolver` / `parse_sai_uri`）
- 解決 API: `GET /api/uri/resolve?uri=<uri>&persona_id=<id>`

## 形式

```
saiverse://self/{resource_type}/{path}?{params}          # 実行中ペルソナ (self)
saiverse://{city}/{persona_name}/{resource_type}/{path}  # city/名前で指定
saiverse://{global_scheme}/{path}?{params}               # グローバル (下記)
```

`persona_id` は `{name}_{city}`（例 `air_city_a` = name=air, city=city_a）。

## グローバルスキーム（persona 非依存）

| スキーム | 形式 | 内容 |
|---|---|---|
| `image` | `saiverse://image/{filename}` | 画像ファイルのパス |
| `document` | `saiverse://document/{filename}` | ドキュメントファイルの本文 |
| `item` | `saiverse://item/{item_id}` | アイテム情報 |
| `item` | `saiverse://item/{item_id}/content?lines=10-50` | アイテム本文（`picture` は画像 URL、`document` は本文。`lines` で行範囲） |
| `persona` | `saiverse://persona/{id}/image` / `saiverse://persona/self/image` | ペルソナのアバター等のリソースパス |
| `building` | `saiverse://building/{id}/items` | Building 内アイテム一覧 |
| `building` | `saiverse://building/{id}/history?last=N` | Building 履歴（直近 N 件） |
| `web` | `saiverse://web?url={encoded_url}&max_chars=N` | Web ページ本文（`read_url_content`） |

## ペルソナスコープ（self の記憶のみ・アクセス制御あり）

`messagelog` / `memopedia` / `chronicle` は**そのペルソナ自身の記憶のみ**参照可能（他ペルソナの記憶は 403 access_denied）。解決には `persona_id` が必須。

### messagelog（過去ログ）

| 形式 | 内容 |
|---|---|
| `saiverse://self/messagelog/msg/recent?depth=N` | 直近 N 件のメッセージ |
| `saiverse://self/messagelog/msg?contain=TEXT&window=N` | TEXT を含む最新メッセージ ± N 件 |
| `saiverse://self/messagelog/msg/{message_id}?window=N` | 指定メッセージ ± N 件 |
| `saiverse://self/messagelog/range?from={ts}&to={ts}` | 時間範囲（UNIX 秒） |
| `saiverse://self/messagelog/thread/{suffix}?last=N` | 指定スレッドの直近 N 件 |
| `saiverse://self/messagelog/summary/{uuid}` | サマリメッセージ |

### memopedia（知識グラフ）

| 形式 | 内容 |
|---|---|
| `saiverse://self/memopedia/tree` | ページのツリー（Markdown） |
| `saiverse://self/memopedia/page/{page_id}` | ページ本文 |
| `saiverse://self/memopedia/page?title=...` | タイトルでページ検索 |

### chronicle（あらすじ）

| 形式 | 内容 |
|---|---|
| `saiverse://self/chronicle/entry/{entry_id}` | 指定エントリ |
| `saiverse://self/chronicle/entry?contain=TEXT` | TEXT を含むエントリ |
| `saiverse://self/chronicle/range?from={ts}&to={ts}` | 時間範囲 |
| `saiverse://self/chronicle/recent?depth=N` | 直近のあらすじコンテキスト |

## 複数解決とトリム

`UriResolver.resolve_many(uris, max_total_chars=8000, priority="first"|"balanced")` で複数 URI をまとめて解決し、合計文字数を制限内に収める（`first`=先頭優先、`balanced`=均等配分）。

## 関連

- [concepts/saimemory.md](../concepts/saimemory.md) / [chronicle.md](../concepts/chronicle.md) / [memopedia.md](../concepts/memopedia.md) — 参照先の記憶レイヤー
- [concepts/item.md](../concepts/item.md) — アイテム
