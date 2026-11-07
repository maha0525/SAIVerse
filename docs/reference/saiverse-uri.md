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
| `item` | `saiverse://item/{item_ref}` | アイテム情報（`item_ref` = 安定 short_id。短縮参照 `item:N` の N。UUID も裏方フォールバックで受理） |
| `item` | `saiverse://item/{item_ref}/content?lines=10-50` | アイテム本文（`picture` は画像 URL、`document` は本文。`lines` で行範囲） |
| `persona` | `saiverse://persona/{id}/image` / `saiverse://persona/self/image` | ペルソナのアバター等のリソースパス |
| `building` | `saiverse://building/{id}/items` | Building 内アイテム一覧 |
| `building` | `saiverse://building/{id}/history?last=N` | Building 履歴（直近 N 件） |
| `building` | `saiverse://building/{id}`（サブパス無し） | **画面の導線専用**（2026-09-12〜）。移動イベントの部屋名リンクに使い、チャット UI がクリックで閲覧先をその Building に切り替える。`UriResolver` で解決しても中身は無く、従来どおり `Unknown building path` エラーが返る |
| `web` | `saiverse://web?url={encoded_url}&max_chars=N` | Web ページ本文（`read_url_content`） |

## ペルソナスコープ（self の記憶のみ・アクセス制御あり）

`message` / `memopedia` / `chronicle` は**そのペルソナ自身の記憶のみ**参照可能（他ペルソナの記憶は 403 access_denied）。解決には `persona_id` が必須。

> **参照アドレッシング統一（2026-07）**: 種類下の余分な階層（旧 `messagelog/`・`chronicle/entry/`・`memopedia/page/`）を平坦化し、`message` / `chronicle` / `memopedia` 直下にキーを置く形へ統一した。短縮参照（`memopedia:N` 等）とこの URI は相互変換でき、同じ実体を指す。過去の自然文に埋まった旧書式リンクは移行せず不活性になる（構造化データのみ移行）。

### message（過去ログ）

`message` は単一メッセージ（`{message_id}` = UUID）とクエリ面（`msg/…` などの検索ナビ）の両方を担う。

| 形式 | 内容 |
|---|---|
| `saiverse://self/message/{message_id}?window=N` | 指定メッセージ（± N 件） |
| `saiverse://self/message/msg/recent?depth=N` | **いま見えていない**メッセージのうち新しい方から N 件（下記「提示済みの除外」） |
| `saiverse://self/message/msg?contain=TEXT&window=N&limit=N&order=new\|old` | TEXT を含むメッセージを `order` の向きに `limit` 件（既定 1・最大 20）。各当たりに ± window 件 |
| `saiverse://self/message/range?from={ts}&to={ts}` | 時間範囲（UNIX 秒） |
| `saiverse://self/message/thread/{suffix}?last=N` | 指定スレッドの直近 N 件 |
| `saiverse://self/message/summary/{uuid}` | サマリメッセージ |

**提示済みの除外（2026-11-07）**: ペルソナがスペルとして解決したとき、`msg/recent` と `msg?contain` は「いまプロンプトに載っている行」（スペルループが tools.context に置く送信中 messages の行 id）と「このスペルを唱えた Pulse 自身の行」を結果から外す。見えているものを返しても想起にならないため。API / CLI 経由の解決ではこの文脈が無いので除外は掛からない。

### memopedia（知識グラフ）

| 形式 | 内容 |
|---|---|
| `saiverse://self/memopedia/tree` | ページのツリー（Markdown） |
| `saiverse://self/memopedia/{page_ref}` | ページ本文（`page_ref` = short_id。短縮参照 `memopedia:N` の N。UUID も裏方で受理） |
| `saiverse://self/memopedia?title=...` | タイトルでページ検索 |

### chronicle（あらすじ）

| 形式 | 内容 |
|---|---|
| `saiverse://self/chronicle/{entry_id}` | 指定エントリ（`entry_id` = UUID） |
| `saiverse://self/chronicle?contain=TEXT` | TEXT を含むエントリ |
| `saiverse://self/chronicle/range?from={ts}&to={ts}` | 時間範囲 |
| `saiverse://self/chronicle/recent?depth=N` | **いま見えていない**細かいあらすじ（Lv1）を新しい方から N 件。head のあらすじ枠（memory weave の選定を現時点の記録で再現）と、提示ウィンドウで digest として見えている畳んだ範囲を「見えている」とみなして除外する。manager / ペルソナ文脈が無い経路では除外なしで新しい Lv1 を N 件 |

## 複数解決とトリム

`UriResolver.resolve_many(uris, max_total_chars=8000, priority="first"|"balanced")` で複数 URI をまとめて解決し、合計文字数を制限内に収める（`first`=先頭優先、`balanced`=均等配分）。

## 関連

- [concepts/saimemory.md](../concepts/saimemory.md) / [chronicle.md](../concepts/chronicle.md) / [memopedia.md](../concepts/memopedia.md) — 参照先の記憶レイヤー
- [concepts/item.md](../concepts/item.md) — アイテム
