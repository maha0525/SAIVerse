# Track（退役済み）

> 🪦 **Track はランタイムとしては存在しない**（2026-08-22、v0.3「形の層」束 6c で完了）。`saiverse/track_manager.py` はモジュールごと削除され、本番コードに `TrackManager` / `manager.track_manager` への参照はゼロ。**新しいコードは Track を一切参照しない。**
> このページは「旧データを読む人」と「まだ Track を前提にした古い設計文書を読む人」のために残してある。撤廃の経緯は intent [`track_retirement.md`](../intent/track_retirement.md)、後継の設計は [`autonomous_behavior_v3.md`](../intent/autonomous_behavior_v3.md)。

## Track とは何だったか

**「ペルソナがいまやっていること」を全部入れる器**として v1 で生まれた概念。作業も、ユーザーとの会話も、関心事も、他ペルソナとの交流も、ひとつのテーブル（`action_track`）の行として表され、その行の状態（`running` / `pending` / `alert` …）がペルソナの現在を表現していた。

**なぜ消したか**: 一枚岩に見えて、実際には別々の概念が固まった合金だったから（[`life_concept_map.md`](../intent/persona_cognition/life_concept_map.md) §10）。「いま何をしているか」「今日の予定」「何に興味があるか」「誰と話しているか」は、それぞれ寿命も持ち主も違う。一つの器に押し込んだ結果、**一回きりの用事が常設の走路になり、尽きたら新しい走路の創発まで強いる**という v1 最大の病理を生んだ（v3 §2）。

## 仕事の行き先

| Track が担っていたもの | いまの持ち主 |
|---|---|
| いまユーザーと会話中か | `saiverse/user_conversation.py`（プロセス内の会話状態。始まり／終わりは Building ログの遷移の一行として実在する） |
| 会話の終わり（応答待ちのタイムアウト） | 同上の沈黙タイマー |
| 今日の予定 | 時間割（`saiverse/day_plan.py`）→ v0.4 でティックへ世代交代（v3 §5） |
| 関心・やりたいこと | **手帳のアクティビティ**（`sai_memory/memory/pocketbook.py`。v3 §13.1） |
| 約束・依頼・期限のある仕事 | **タスク帳**（`saiverse/task_book.py`。v3 §4.1） |
| 割り込み（alert 状態機械） | イベント到着判断（on_event）への直結 |
| Track 操作スペル（`track_*`）と v1 メタ判断 | 退役（2026-08-14 / 2026-08-21） |
| ゲーム参加中かの帳簿 | `region.state`（`is_participating`） |

## 旧データを読むときに知っておくこと

**データは消していない。** v3 §9-8 の裁定「旧データは残置して壊さない。削除はいつでもできる」に従い、以下はそのまま残っている:

| 残っているもの | 状態 |
|---|---|
| `action_track` テーブル（`ActionTrack` モデル）と既存の行 | **読み取り専用の残置。** 書き手ゼロ。読むのは記憶ブラウザ（`api/routes/people/storage_layers.py`）と、一回きりの写し替え（`saiverse/v3_shape_migration.py` が題を手帳のアクティビティへ複製する）だけ |
| `messages.origin_track_id` / `building_messages.origin_track_id` の列と既存の値 | **書き手は 2026-08-21 に全撤去。** 新しいメッセージには付かない。読み手は旧データ向けに生存（あらすじの絞り込みフィルタ・Pulse タイムライン・記憶ブラウザ・`scripts/inspect_world.py`・native export） |
| `track:N` という参照（旧時間割のコマ `ref`・`purpose_tags` の指し先・旧あらすじの metadata） | **解決できない参照として扱われる。** 表題の解決器（`judgment_finalize._ref_label`）は素の文字列へ縮退し、想起の歩き（`recall_walk`）は「解決できない参照」として扱い、時間割の検証（`day_plan`）は**文法としては受理し続ける** — 旧データの `track:N` を「不正な ref」に化けさせてコマごと壊さないため |
| `track_local_log` / `meta_judgment_log` テーブル | 同じく読み取り専用の残置 |

**新しい `track:N` はどこからも生まれない。** 見かけたら必ず 2026-08-21 以前のデータ。

## 撤廃の道のり

| 消えたもの | 時期 |
|---|---|
| alert 状態機械 / v1 メタ判断一式 | 2026-08-14（撤去順序①） |
| Track 操作スペル（`track_*`）と deferred ops | 2026-08-21（束 6 第二便） |
| Track 種別ごとの Handler 三種と `on_track_activated` hook | 2026-08-21（束 6 第三便） |
| ユーザーとの会話の器 | 2026-08-21（束 6 第三便） |
| メッセージへの Track 刻印（`origin_track_id`）の書き手 | 2026-08-21（束 6 第三便） |
| ゲーム参加の帳簿 | 2026-08-21（束 6 第三便） |
| **`TrackManager` 本体（`saiverse/track_manager.py`）と最後の読み手 8 箇所** | **2026-08-22（束 6c）— これで概念として消えた** |

テーブルと既存データの掃除（migration）は撤去順序⑦で、**まだやっていない**。

## 関連

- intent: [`track_retirement.md`](../intent/track_retirement.md)（撤廃計画と住人台帳）/ [`persona_action_tracks.md`](../intent/persona_action_tracks.md)（当時の設計。**現状とは一致しない歴史文書**）
- 後継: [`autonomous_behavior_v3.md`](../intent/autonomous_behavior_v3.md) §4.1（台帳三つ）/ §13.1（手帳とアクティビティ）
- 地図: [`landscape.md`](../overview/landscape.md) §9（死んだ概念）
