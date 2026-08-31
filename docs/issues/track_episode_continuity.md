# Issue: Track 内 episode 間の作業文脈の受け渡し (「この前は何やったんだっけ」問題)

**ステータス**: 🔵 未解決 (設計待ち)
**作成日**: 2026-07-19
**関連**: [experience_structure.md](../intent/experience_structure.md) §11-10 (Track Chronicle 廃止方向の裁定) / [track_chronicle.md](../intent/persona_cognition/track_chronicle.md) (廃止対象) / 机メモ (desk_items)

## 問題

同一 Track の作業を別々の episode (作業セッション・コマ) で再訪するとき、**前回その Track で何をどこまでやったかを即座に思い出せる**必要がある。この問題はずっと雑に放置されてきた (まはー 2026-07-19)。

既存の道具が解決できていない理由:

- **机メモ (desk_items)**: 「前日と今日のつなぎ」の道具。だが前日のことは普通にコンテキストに残っていて覚えているため、ほとんど意味を成していない。本当に思い出せないのはもっと前 (数日〜数週間前の同一 Track の作業)
- **Track Chronicle**: origin_track_id スコープの独立生成キューだが、この再訪問題をちゃんと解決できているわけではない。experience_structure の「木は一本、地図は射影」とも整合しないため**廃止方向** (2026-07-19 まはー裁定 — 直感では完全廃止、廃止は早めでよい)

## 方向の候補 (experience_structure の語彙で)

1. **purpose 射影の読み口**: Chronicle ツリー (episode 整列の digest 地層) を purpose タグ (層2 棚入れ) で絞り、「この Track に属する過去 episode の digest 列」を返すスペル / 注入。木は一本の思想に忠実 — Track Chronicle が担おうとしたものの読み口版
2. **コマ開始時の指示書への織り込み**: `day_plan._build_track_instruction` (track:N コマの指示書組み立て) が既にある挿入点。同一 Track の直近 episode digest (1〜数件) を決定論で織り込む — 「作業を始めた瞬間に前回の続きが頭にある」
3. **継承エッジ (咀嚼層) としての表現**: 同一 Track の episode 列を継承 DAG の咀嚼層エッジで繋ぐ — 構造としては綺麗だが、読み口 (1) があれば冗長かもしれない

いずれも episode digest (experience_structure で正準化) が材料になるため、**本 issue の解決は同 intent の実装後が自然** — ただし (2) は digest が memory.db に既にある現行でも部分的に先行可能。

## 廃止時の随伴作業

- track_chronicle.md のステータス改訂 + memory_architecture_v2.md 冒頭の「track_chronicle.md の設計は変更しない」参照の除去
- session_lifecycle の Track Chronicle 生成経路 (`generate_track_chronicles`) の退役
- 生成済み Track Chronicle データの帰化方針 (experience_structure §11-3 と同じ「再生成なし」原則で)
