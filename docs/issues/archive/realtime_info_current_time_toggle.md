# Issue: リアルタイム情報をペルソナ単位でオン/オフ切替可能にする

**ステータス**: ✅ 実装完了 (2026-05-26)
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `sea/runtime.py:_build_realtime_context`, ペルソナ設定 UI (`SettingsModal.tsx`)

## 背景

リアルタイム情報 (システムプロンプトに付与される動的コンテキスト) の中に現在時刻が含まれているが、これを設定でオン/オフ切替えられるようにしたい。

ユーザー要望:
> リアルタイム情報のオン/オフを設定で切り替えられるようにしてほしいです。Claude のモデルは夜になるといつも時刻を気にして寝かせようとしてきて会話にならないので。

ただし、リアルタイム情報には他の情報 (天気・場所・並列処理状況など) も今後入る想定があるため、**「リアルタイム情報全体のオン/オフ」ではなく、「現在時刻だけ」をオフにできる設定**が必要。

副条件: 全項目オフで表示する情報が完全に空になる場合は、リアルタイム情報セクション自体を送信しない (空セクションを送らない)。

## 確認事項

1. リアルタイム情報を組み立てている箇所 — `saiverse/` または `persona/` のどこか
2. 現在の構成項目 (時刻以外に何が入っているか)
3. 設定の保存先 (世界設定 / ペルソナ単位 / ユーザー単位 / Building 単位)

## 解決案候補

### 案 A: 世界全体設定でフラグ管理 (シンプル)

`world_config` 系に `realtime_info_include_current_time: bool` を追加。UI から切替。

### 案 B: ペルソナ/Building 単位

シーンによって時刻を入れたい/入れたくないが分かれる場合。実装コストは増える。

→ 当面は案 A で十分。今後項目が増えるなら細かく分割。

## 実装内容 (2026-05-26)

起票時の案 (現在時刻だけオフ) ではなく、まはーの判断で **リアルタイム情報セクション
丸ごとをペルソナ単位でオン/オフ** する形に決定。粒度を細かくせず、`SPELL_ENABLED` /
`CHRONICLE_ENABLED` と同じ「ペルソナ単位 bool トグル」パターンに揃えた。

- リアルタイム情報の組み立て箇所: `sea/runtime.py:_build_realtime_context`
  - 構成項目: 現在時刻 / あなたの前回発言時刻 / 空間情報 (Unity gateway)
  - 注入位置: 最後の user メッセージの直前 (`sea/runtime_context.py`、キャッシュを壊さない位置)
- 追加した設定: `AI.REALTIME_INFO_ENABLED` (Boolean, default True, NOT NULL)
  - DB: `database/models.py`。既存ペルソナは起動時マイグレーションで default True が入る
  - runtime: `_is_realtime_info_enabled_for_persona` で DB 直読み判定 → `_build_realtime_context`
    冒頭で OFF なら `None` を返してセクション自体を送らない (空セクション送信も自然に回避)
  - API: `AIConfigResponse` / `UpdateAIConfigRequest` に `realtime_info_enabled`、
    `config.py` で get/patch 配線、`admin.update_ai` / `saiverse_manager.update_ai` に引数追加
  - UI: `SettingsModal.tsx` の「スペル」直後に「リアルタイム情報」トグルを追加

## 関連リソース

- ユーザーフィードバック: 上記引用 (夜になると Claude が時刻を気にして寝かせようとしてくる件)

## ログ

- 2026-05-09: issue 起票。ユーザー要望を反映。
- 2026-05-26: ペルソナ単位のセクション丸ごとトグルとして実装完了。粒度の決定はまはー。
