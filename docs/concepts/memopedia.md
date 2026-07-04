# Memopedia（知識グラフ）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §5](../overview/landscape.md)、**設計意図**は intent [`memopedia_knowledge_graph.md`](../intent/memopedia_knowledge_graph.md)、**ユーザー向け**は [user-guide/memopedia.md](../user-guide/memopedia.md) を参照。

## 一言で

会話に登場した固有の対象（人物・AI・プロジェクト・概念）を、ページとして整理する知識グラフ。

## 役割

生ログや [Chronicle](chronicle.md) は時系列だが、Memopedia は**対象（エンティティ）ごと**に知識を集約する。「あの人物について何を知っているか」「このプロジェクトの経緯は」を、時系列を横断して引ける形にする。

## 仕組み

- `entity_extractor` が会話からエンティティを認識
- 各エンティティの知識を **Fragment**（知識の最小単位）として抽出・追記
- ページは **summary（一文定義）+ content + Fragment 群** で構成
- 固有名詞を title とした親子構造を持つ

### Fragment の生成タイミング（検証済）

Fragment は単独では生成されない。**[Metabolism](metabolism.md) 発火時に `ArasujiGenerator` が [Chronicle](chronicle.md) を生成する各バッチで、`batch_callback` として `entity_extractor` が相乗りして Fragment を生成する**。

> これが「Chronicle 二重パイプライン統合」の実体。**記憶の「圧縮（Chronicle）」と「知識化（Fragment）」は Metabolism という同じ節目で連動する。**

### 実装状況メモ

- air_city_a 実 DB で `memopedia_fragments` は稼働中（1000件超）
- Fragment の embedding は Metabolism 時に生成される（`sea/runtime.py:2313` 付近で `entity_extractor` が Fragment を作った後、`sea/runtime.py` の `embed_memopedia_fragments`（`sai_memory/unified_recall.py`）が未 embedding の Fragment に埋め込みを付与）。生成された embedding は `unified_recall`（`sai_memory/unified_recall.py:717` 付近、`search_fragments` 経路）が検索に使用する
- 旧 `note_extractor.py` は本番 Metabolism 経路から呼ばれない。現行は `entity_extractor`（併存は移行の名残）

## 実装

- 抽出: `sai_memory/memory/entity_extractor.py`
- ストレージ/コア: `sai_memory/memopedia/storage.py` / `core.py` / `generator.py`
- テーブル: `memopedia_*`（pages / fragments 等）
- 生成の相乗り: `ArasujiGenerator.batch_callback`（`sai_memory/arasuji/generator.py`）

## 関連概念

- [SAIMemory](saimemory.md) — Memopedia を内包する容れ物
- [Chronicle](chronicle.md) — 同じ Metabolism バッチで連動生成
- [Metabolism](metabolism.md) — Fragment 生成を発火する節目

## 参照

- intent: [`memopedia_knowledge_graph.md`](../intent/memopedia_knowledge_graph.md)
- ユーザー向け: [`user-guide/memopedia.md`](../user-guide/memopedia.md)
- 地図: [`landscape.md`](../overview/landscape.md) §5
