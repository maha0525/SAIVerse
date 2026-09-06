# 部屋に戻ると、head のアイテム一覧と同じ内容が知覚にも全文で積まれる

**ステータス**: ✅ 実装済み (実機検証待ち) — v0.3.9 に追加実装 (2026-09-05 まはー裁定「今直したい」)
**深刻度**: 中 — アイテムの多い部屋 (実測 41 件) では、一往復の帰還ごとに巨大な一覧が二重化する。v0.3.9 の知覚上限が総量の天井は張るが、重複自体は残っていた
**発見**: 2026-09-05 (エリスの実機ツアー。経路 = まはーの部屋 → エリスの部屋 → まはーの部屋、で head 最上部とコンテキスト最下部に item:252〜442 が一字も違わず二重出現)
**関連**: [persona_recall_perception_unbounded.md](persona_recall_perception_unbounded.md) (v0.3.9 の束) / [watermarks_unsatisfiable_when_perception_is_large.md](watermarks_unsatisfiable_when_perception_is_large.md) §2026-09-04 の議論 ④ (アイテム説明の見え方・表面アイテム数の上限) / 設計は [perception_buffer.md](../intent/perception_buffer.md) §10.8.1

## 何が起きるか

1. ペルソナが部屋 A にいる — head (凍結された文脈の頭) に部屋 A のアイテム一覧が載る。
2. 部屋 B へ移動して戻ってくる — 入室時の「部屋の様子」知覚として、部屋 A の全アイテム概要が**全文**で積まれる。
3. head の一覧と知覚バッチが同一内容の二重になり、編纂か知覚上限で下りるまで送信され続ける。

## v0.3.9 の差分機構が効かなかった理由

v0.3.9 の「再訪なら差分だけ積む」は、**知覚バッチ同士** (同部屋の前回バッチと今回) の比較で重複を消す。上の経路では「同部屋の前回バッチ」が存在しない (head に載っているだけ) ため初回全文の扱いになり、head との重複は照合の対象外だった。

## 実装の記録 (2026-09-05)

### 事実確認 (head 側)

- 「現在の部屋のアイテム一覧」を見せているのは **`VisualContextSection`** (`sea/head_pipeline/sections/visual_context.py`、order 800)。`BuildingItemsSection` は head に何も描かない (差分通知の素材だけ)。
- この Section は **移動では撮り直されない** (`refresh_on_events = {APPEARANCE_CHANGED}`)。移動で撮り直すと head が変わって prompt cache が壊れるため、意図的にそうなっている。撮り直しは Metabolism (`dispatch_event(METABOLISM)` → `capture_all`) と anchor TTL 切れ (`ensure_snapshot`) のとき。
- したがって A → B → A の往復では、head はその間ずっと A の姿を見せている。入室 push (`saiverse/dynamic_state.on_building_entered`) は head より後に起きるが、head の中身は変わらない — まはーの実測どおり、両方が同じ A の全文を見せる。
- head は **(ペルソナ, model) ごと**に別の行 (`session_head_snapshot`)。実 DB では 1 ペルソナに最大 10 行 (エリス) あり、capture 時刻は数週間ばらける。つまり「head がどの部屋を見せているか」は model ごとに違う。

### 採った形

要点は intent [perception_buffer.md §10.8.1](../intent/perception_buffer.md) に置いた。ここには裁定と当たりとの差分だけを書く。

- **土台は head の姿** (メインの当たりどおり)。ただし head 用の姿と入室 push の姿は書式が違う (`<system>` 包み・自分の外見・インベントリの有無) ので、`VisualContextSection` の capture が **同じ瞬間の知覚記法の姿** (`room_text`) と `building_id` も焼くようにした。head 用の姿から機械的に削る案は採らない — `get_visual_context` の出力書式に依存する第二の parser になる。
- **「同じなら積まない」ではなく一行だけ積む** (当たりとの差分)。head は (ペルソナ, model) ごとに別の時点を見せるので、「default model の head が見せている」ことは他の model の head が見せている保証にならない。一行 (`前回見たときから変わっていません。`) を積んでおけば、head が別の部屋を見せている model にはそれを全文へ開き直せる。一行も積まないと、その model には部屋の記録が一切残らない (2026-07-09 に塞いだ穴が開き直す)。
- **chain に入れるか独立させるか → 独立** (当たりの二択の後者)。head 土台の差分は台帳の連なりに参加しない: `chain_is_intact` の照合に出さず、後続の差分の土台にもしない (`latest_visible_snapshot` は直近が head 土台なら None を返し、次は全文を積む)。連なりを台帳だけで閉じたまま保てるので、「土台の可視性が knowledge 列の外で変わる」問題を chain へ持ち込まずに済む。
- **開き直しは提示時だけ、条件は「head が別の部屋を見せていること」**。同じ部屋なら head が撮り直されていても差分のまま出す — 全体像は撮り直した head が最新の姿で見せているし、開き直すと同じ部屋の全文が二枚並ぶ。判定は台帳へ書けない (model ごと) ので、Chronicle 無効の窓絞りと同じく `reopen_lost_bases` に置き、同じ head の値を下ろし量の見積もりへも渡して勘定と実送信を揃えた。

### 変更したもの

| ファイル | 変更 |
|---|---|
| `builtin_data/tools/get_visual_context.py` | 世界の読みと描き分けを分離 (`build_visual_contexts`)。head 用の姿と知覚記法の姿を**一度の読み**から作る |
| `sea/head_pipeline/sections/visual_context.py` | snapshot に `building_id` / `room_text` (知覚記法) を追加。capture が一度の読みで両方を焼く。旧行は `None` / `""` で復元 |
| `sea/head_pipeline/integration.py` | `current_head_room()` — 撮り直さずに「head が今見せている部屋」を読む口。`head_room_of_snapshot()` と `render_head_messages(head_room_out=)` — 描画で pin した head の部屋を持ち帰る口 |
| `saiverse/dynamic_state.py` | 入室 push で `_head_view_of()` を通し、同じ Building のときだけ head の姿を渡す。読むのは**入室処理の最初** |
| `saiverse_memory/adapter.py` | `push_room_state(..., head_full_text=)` の受け渡し |
| `sai_memory/room_state.py` | head 土台の記帳 (`base_source="head"`)、連なりからの除外、提示時の開き直し規則 |
| `sea/runtime_context.py` | 描画済み head の部屋 (`_pinned_head_room_key`) を開き直しと下ろし量の見積もりの両方へ渡す。head を組まない呼び出しだけ `_head_room_key()` で今の head を読む |
| `tests/test_room_state_diff.py` | 往復経路・変化あり/なし・model ごとの開き直し・勘定の一致・head 側の供給・入室が作った head を土台にしないこと・描画済み head の持ち帰り・読みが一度であること |

### Codex レビューで直した 3 件 (2026-09-05)

いずれも「同じものを別の瞬間に二度読む」型で、初版は三箇所でこれをやっていた。

1. **入室処理が自分で作った head を土台にしていた** (high)。`_head_view_of` を `inject_diff_notifications` の**後**に呼んでいたため、snapshot 未構築 / anchor TTL 切れの回は `ensure_snapshot` が移動先で `capture_all` し、その head を土台に選んでいた。初訪問でも「変わっていません」の一行になり、見たことのない部屋に「前回」があったことにされる。head の姿を入室処理の先頭で確定させて直した。
2. **提示判断が「実際に送る head」と別の head を見ていた** (high)。`prepare_context` は head を先に描画して固定するのに、知覚の提示側は後段で `current_head_room` を読み直していた。間に Metabolism / TTL の capture が走ると、prompt に載る head と判定に使う head が別物になる。`render_head_messages` が pin した snapshot から部屋を持ち帰る out-param (`head_room_out`) を足し、その call-local の値を計画・開き直し・送信へ一貫して渡すようにした。
3. **head 本文と知覚記法の姿を別々の瞬間に読んでいた** (medium)。capture が `get_visual_context` を二回呼んでいた。`build_visual_contexts` で世界の読みを一度にし、姿ごとの違いは組み立てだけにした (出力は旧実装と一字一句同じことを 10 通りの構成で確認済み)。

### 残っている境界

- **入室直後の Metabolism で作られる二重は残る**。初訪問で全文を積み、その Pulse の末尾で head が同じ部屋を capture すると、head と知覚が同じ部屋の全文を二枚見せる。消すには確定済みバッチの文面を縮める必要があり、§10.8 の移管 (差分の土台としての全文) と真正面からぶつかる。
- **model ごとの「前回」のずれ**: model Y の head が同じ部屋の別の時点を見せている回は、差分の「前回見たとき」が Y の見ている姿とずれる。全体像は Y の head にあり二重も無いので受容。
- **head をまだ一度も組んでいない Session** (再起動直後の最初の勘定など) では head が引けず、head 土台の差分は全文へ開き直される (安全側)。

## 経緯

- **2026-09-05**: v0.3.9 実装 (知覚の堆積対策 6 件) の実機検証ツアー中に発見・起票。同日、まはー裁定で v0.3.9 に追加実装。同日の Codex レビューで「同じものを別の瞬間に二度読む」型の欠陥 3 件を消し込み。
