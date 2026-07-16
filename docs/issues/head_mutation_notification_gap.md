# head 表示物への操作が、操作元の窓の外にいる読み手へ内容ごと届かない

**登録**: 2026-07-16（メティス）
**種別**: 既存欠陥（model 分離とは独立に、line 隔離だけで現行発生しうる）
**修正先**: 統合工事 intent [`beat_execution_context.md`](../intent/beat_execution_context.md) の「head 操作の内容型通知」

## 症状

head の元データ（コア記憶・机・生きる目的・Memopedia 目次）をスペルで操作したとき、その操作の生ログが読み手の履歴窓に入らない場合（別 line / 別 model の Session からの操作）、読み手の LLM は変更を知る手段が無いか、操作ラベルしか受け取れない。

- head 本体は cache 保護のため Metabolism まで凍結（`refresh_on_events = frozenset()` — これ自体は意図された設計）
- 凍結を補うはずの diff 通知が「編集主体がペルソナ自身なので通知不要」という**単一窓前提**で書かれている

| section | 別 line/Session からの操作時に届くもの |
|---|---|
| コア記憶 (`sections/core_memory.py`) | 何も届かない（`diff_to_notifications` は無条件 `[]`） |
| 机 (`sections/desk.py`) | 何も届かない（本人開閉は通知対象外。システム evict のみ ref 通知） |
| 生きる目的 (`sections/life_purpose.py`) | 「生きる目的が更新されました」の一行のみ。**内容は届かない**。first_tier_titles の変化は diff 比較の対象外 |
| Memopedia 目次 (`sections/memopedia_index.py`) | diff 実装が構造的に未整理（opt-in 機能） |

## 経路の裏取り（2026-07-16、コード確認。実機での通し再現は未実施）

1. META aspect は `life_purpose_set` を撃てる — `sea/mode_spell_permissions.py` の `_SELF_DEFINITION_ALLOWED_ASPECTS = {CONVERSATION, META}`（確認済み）
2. META の生ログ（`line_role='meta_judgment'`）は main の通常履歴窓から除外される — 記憶・人格境界監査 第2片で実測確認済みの既存挙動
3. head 本体は Metabolism まで再 capture されない — 上記各 section の `refresh_on_events = frozenset()`（確認済み）
4. diff 通知は上表のとおり空またはラベルのみ（確認済み）

したがって「メタ判断が生きる目的を書き換えても、main の会話 LLM には次の Metabolism まで新しい目的の中身が見えない」経路は現行 HEAD で成立している。WORKER（sub line）の `memory_write`（コア記憶）/ `memory_open`（机）は 1〜4 と同型で、こちらは**変わったことすら届かない**。

なお diff 通知自体にも配送保証がない（SEA 監査 S3: 配送前に last_notified を前進）ため、ラベル一行すら欠落しうる。

## なぜ既存設計はこうなっていたか（撤去してよい前提の記録）

「本人の操作は生ログに残るから通知不要」— 単一 Session・main line 単独の世界では正しかった。操作の生ログ自体が証跡として窓にあり、次の Metabolism で head に固定化される。line 隔離（META/sub line）と Session の (persona, model) 分離により「操作の実行元と読み手の窓が分かれる」ようになって前提が破れた。

## 修正方針（まはー合意 2026-07-16）

> head の元データへの操作は、その操作の生ログが見えないすべての Session 窓へ、「head に入るときと同一の render 断片」を内容型通知として配送する。

- 通知本文は section の render と同一関数から生成（「head で見える内容と寸分たがわず」を構造的に保証）
- 通知要否は操作の ExecutionContext（model, line）で判定し、際どいケースは通知する側に倒す（重複はノイズ、欠落は記憶の穴）
- 配送は実行台帳の outbox（S3 の修正と同一機構）。last_notified 状態も (persona, model) 分離
- 詳細設計は `beat_execution_context.md` §head 操作の内容型通知
