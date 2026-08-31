# Issue: ツール指定→スペル指定で話しかけた際、スペル使用分の Usage がチャット UI に表示されない

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-09
**関連**: チャット UI の Usage 表示, スペル (Playbook) 実行経路

## 背景

チャット UI 上では、メッセージごとに使用したトークン数 (Usage) が表示される。
しかし「ツール指定 → スペルを指定して話しかけた」場合、スペル実行中の LLM 呼び出し分の Usage がチャット UI に反映されない。

API 使用状況 (集計画面側) には載っているかもしれないが、メッセージ単位の Usage 表示には来ない。

## 確認事項

1. 通常チャットで Usage が表示される経路 (どこで集計してフロントに流しているか)
2. スペル実行時の LLM 呼び出しがその経路を通らない理由
3. SEA Runtime の playbook ノード実行時に、Usage を集約してメッセージ Usage に加算する仕組みがあるか

## 解決案候補

- SEA Runtime の LLM ノードで返ってくる Usage を pulse / メッセージ単位で合算し、最終応答に乗せる
- ストリーミング応答に Usage 増分イベントを混ぜて UI 側で逐次加算

## 関連リソース

- `sea/runtime.py` LLM ノード実行
- チャット API (`api/`) のストリーミング応答
- `frontend/` チャット UI の Usage 表示

## ログ

- 2026-05-09: issue 起票。画像生成 API カウント漏れ ([image_generation_api_usage_tracking.md](image_generation_api_usage_tracking.md)) と関連あり (Usage 集計の網羅性問題)。
