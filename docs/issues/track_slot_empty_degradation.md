# Issue: track:N コマが空 Track で無音縮退 — day_open の約束と実装の不一致

**ステータス**: ✅ 解決 (2026-07-19、方向1 で修正・回帰固定) — 実機再検証待ち
**作成日**: 2026-07-19
**関連**: [autonomous_v2_post_live_gaps.md](autonomous_v2_post_live_gaps.md) 束B (世界に向く last mile) の親戚 / `saiverse/day_plan.py` `run_worker_slot_session` / [track_episode_continuity.md](track_episode_continuity.md) (同じ Track 再訪の文脈)

## 解決 (2026-07-19、方向1)

まはー裁定で方向1 (note を指示書に昇格) を採用・実装。急所は `_build_track_instruction` が **note を縮退判定の後で読んでいた**こと — 判定が `live_tasks` と `memo` しか見ず、note を読む前に `return None` していた。修正:

- 縮退条件を `not live_tasks and not memo and not note` に変更 (生存タスク・机メモ・コマの覚え書きが**すべて**空の Track だけ presence 縮退)。note があればそれを目標にセッションを回す
- 生存タスク一覧が無く note/机メモだけで回る経路のために、指示書の締め文言を分岐 (「この中から選んで」= タスク一覧を指す語を、タスク不在時は「意図と覚え書きに沿って取り組む」に切り替え)
- 回帰: `tests/test_track_slot_ref.py` — 真に空の Track は従来どおり縮退 / **note があれば縮退せずセッションが回り、指示書に note の意図が乗る** (実害の直接再現)

空回り防止の本来の意図 (何の意図もない空 Track で LLM 予算を空費しない) は「note も memo もタスクも無い」ケースにだけ残り、無傷。experience_structure の digest/episode 工事とは独立した day_plan 局所修正で完結した。

---

## (以下、当初の記録)

## 実機観測 (2026-07-18, air_city_a)

air は前日にタスクを全部完了させた状態で、day_open が 13:00・16:00 に「作る」コマ (ref=track:7「自律思考・行動テスト」、「まはーが作りたい機能のアイディアや準備を行う」「スタックチャンのランダム動作生成について構想を練る」) を編成した。発火時、両方とも:

```
[day_plan] track slot degraded to presence (empty track): persona=air_city_a date=2026-07-18 index=3 ref=track:7
```

→ セッション実行なしの「時間を過ごした（詳細な記録なし）」に縮退。**「アイディアを練る」つもりだった午後がまるごと無音**になった。

## 問題の構造

- day_open のプロンプトは約束している:「コマの ref には具体的なタスク (task:N) のほか、**関心そのもの (track:N) も指せます — その場合、何をするかはコマが始まる時にその関心の状況を見て決めます**」
- 実装 (`run_worker_slot_session` → `_build_track_instruction`) は、Track 配下に生存タスクが無いと**判断すら走らせず** presence 縮退する (「中身が空の Track は presence 相当に縮退」— docstring に明記された設計仕様)
- つまり約束と実装の不一致。ペルソナは「構想を練る」注記まで書いてコマを置いたのに、実行系はそれを読まずに落とす。縮退自体は空回りセッション (LLM 予算の空費) を防ぐ意図で、悪意ある実装ではない — 「タスクが無い=やることが無い」という等式が誤り (コマの note に意図が書いてある)

## 方向の候補

1. **note を指示書に昇格**: Track が空でもコマの note (「構想を練る」等) があれば、それを目標にしたセッションを回す — ペルソナが書いた意図をそのまま尊重。最小の修正
2. **開始時判断**: 約束どおり「コマが始まる時にその関心の状況を見て決める」判断点 (軽量) を撃ち、やる/流すを本人に決めさせる — 判断 Pulse の予算枠との整合が要る
3. **day_open 時の検証**: 空 Track への ref を編成時に警告して ref=none の暮らしコマへ誘導 — 約束の文言側を実装に合わせる方向 (発火時の自由度は失う)

## 補足

experience_structure の実装で作業セッションの digest / episode まわりが変わるため、対処は W1〜W4 の工事と時期を揃えるのが自然。
