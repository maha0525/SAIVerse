# 出かけるコマでフィード施設に着いても、その Pulse で記事に出会えない

**ステータス: 未解決** (2026-08-08 起票。時間割実機検証中のまはーの疑問「そのBuilding内から始まるPulseじゃないとフィードの中身見えないんじゃ」が的中)

## 症状 (二段)

**① 配送タイミングの構造**: 記事の知覚配送 (`FeedManager.deliver_new_items`) は定期取得サイクル (`_fetch_cycle_worker`、既定 30 分) からしか呼ばれず、**入室トリガーの配送が存在しない**。配送対象は配送時点でその Building にいるペルソナのみ。

- 9:00 に出かけるコマでニューススタンドの Building に到着 → 記事が届くのは早くて次のサイクル
- 出かけるコマの軽い一手 Pulse は到着直後の一回きり → **その Pulse の知覚に記事は無い**
- 滞在がサイクルを跨げば知覚バッファには積まれるが、読まれるのは次の認知機会 (帰宅後のコマ・会話など) — 「スタンドの前で読む」でなく「帰ってから届く」体験になる

**② 設置物欄の見出し表示が未実装疑い**: `feed_manager.update_fixture_display` は Fixture.STATE_JSON の `feed_stand` キーに表示情報 (購読タイトル一覧 + 直近見出し 5 件 + 更新時刻) を書いているが、描画側 `builtin_data/tools/get_visual_context.py` の設置物 (Fixture) 節は STATE_JSON の値を `value_num` / `value_text` 形式 (ObserverManager の観測値形式) でしか拾わない。`feed_stand` の辞書はどちらも持たないため **val=None でスキップされ、何も表示されない**。到着したペルソナに見えるのはスタンドの NAME と DESCRIPTION だけ。

書き手 (feed_manager) は 2026-08-02 夜間分業のエージェント A、読み手 (get_visual_context) は既存コード — 継ぎ目の突き合わせ漏れ。夜間ハンドオフ §2-5 は「visual context に購読タイトル・最新見出しが出ること」を検証項目に挙げており、**実装済みの前提が誤っていた**。

## 直す方向 (未裁定)

1. **入室トリガー配送**: BUILDING_ENTERED (または移動確定点) でその Building の購読の未読をカーソル方式で N 件配送。決定論・安価で、「出かけた先で新聞に出会う」が出かけるコマの Pulse 内で成立する。既存の「現在地再確認 + カーソル commit 先行」の配送関数がそのまま使える形。
2. **設置物欄の描画対応**: `feed_stand` キーを get_visual_context が描画できるようにする (描画側に専用処理を足すか、書き手が value_text 形式に寄せるかは設計判断。STATE_JSON はプロンプト直載りなので上限・文字数はすでに書き手側で制御済み)。

②だけ直しても「見出しは見えるが本文の知覚は時間差」のまま。①②セットで初めて intent の「場所駆動の偶然の供給」が実機の体験になる。

## 実機検証への影響 (2026-08-08 注記)

統合検証手順 Step 3 の「フィード配送」項目は、現状のままだと**サイクル任せの時間差配送が仕様どおりの挙動** — 出かけるコマの Pulse に記事が無くてもバグではない。visual context の見出し確認は②が直るまで検証不能。

## 関連

- [rss_feed_intake.md](../intent/rss_feed_intake.md) (提示の二経路) / [timetable_redesign.md](../intent/timetable_redesign.md) §8 (世界側の供給源)
- [夜間ハンドオフ](../handoff/2026-08-03_rss_and_timetable_night_handoff.md) §2-4/§2-5
