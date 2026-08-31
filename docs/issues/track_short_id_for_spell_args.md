# Track 操作スペルに短縮 ID を導入する

## 状態: 実装済み

## 問題

ペルソナが Track 操作スペル (`track_pause`, `track_complete`, `track_activate` 等)
を発火する際、引数に UUID 形式の `track_id` を指定する必要がある。
LLM は長い UUID を正確に引用できず、ハルシネートする。

### 発生事例 (2026-05-26)

エアの自律 Track `d31a15a7-1293-4a84-adb5-56f68686e408` に対して
`track_pause` を発火したが、渡された track_id は
`d31a15a7-e59a-49b2-af9a-2291b7d08e1a` だった。
UUID の前半 8 文字は正しいが、後半は直前に作成した memopedia ページ ID
(`69a67b5d-e59a-49b2-af9a-2291b7d08e1a`) の後半部分と混同していた。

結果:
- `track not found` で pause が失敗
- Track は running のまま残り、SubLineScheduler が 38 回の Pulse をトリガーし続けた
- 標準モデル × 38 回 = 1000 円以上の無駄な課金が発生

## 解決方針

Item 操作と同じアプローチ: Track にスロット番号的な短縮 ID を発行し、
ペルソナには UUID を見せずに短縮 ID で操作させる。

### 設計案

- ペルソナ単位で Track にセッション内の通し番号 (例: `T1`, `T2`, `T3`) を振る
- システムプロンプトの Track 一覧には短縮 ID のみ表示
- スペル引数は `track_id='T1'` のような形式で受け付ける
- ツール内部で短縮 ID → UUID の解決を行う
- UUID はログやデバッグ UI (Pulse Timeline 等) でのみ表示

### 影響範囲

- `builtin_data/tools/track_pause.py`
- `builtin_data/tools/track_complete.py`
- `builtin_data/tools/track_create.py` (activate 時の track_id 返却)
- `builtin_data/tools/track_list.py` (短縮 ID の表示)
- Track コンテキスト注入 (`autonomous_track_handler.py`, `user_conversation_handler.py`)
- システムプロンプト内の Track 一覧生成

### 参考

- Item のスロット番号: `builtin_data/tools/item_*.py`
