# Building の旧自動インターバル入力を取り除く

**ステータス**: 検証待ち — 合成データでの回帰検査は合格、ブラウザと実機の検証は未完了。
**根拠**: [既存 issue](../issues/building_auto_interval_setting_removal.md) の「Building 単位のインターバル設定はもう不要」「UI ノイズになるので削除する」。旧駆動の退役は [認知モデル Phase 4-d](persona_cognition/phases/phase_4_pulse_scheduler.md) と [landscape §9](../overview/landscape.md) を参照。

## 全体と守ること

利用者には、動作を変えない設定欄を見せない。一方、既存の建物を別の理由で保存しただけで、保存済みの値を変えてはいけない。

`Building.AUTO_INTERVAL_SEC` → 建物一覧 API → Building 設定モーダル / ワールドエディタ → 更新 API → 同じ DB 列、という往復がある。更新 API の `auto_interval` は現在も必須だが、受け取った値が渡る旧 `ConversationManager` の `start` / `stop` / `trigger_next_turn` は no-op。UI の入力だけを取り除き、更新 payload には読み込んだ値をそのまま残す。0 を含む保存済み整数を 10 に丸めない。

## 変更する境界

- `BuildingSettingsModal.tsx` と `settings/WorldEditor.tsx` の入力欄・不要になる翻訳キーを削除する。
- モーダルでは未取得値の既定と保存済みの 0 を区別する。ワールドエディタは読み込み済みの `auto_interval` を引き続き送る。
- DB 列・更新 API の必須項目・バックエンドの検証・自律駆動を変更しない。クラスと列の撤去は参照整理を伴う別の段階。
- 建物の選び直し・閉じて開き直し・保存失敗後の再試行でも、選択対象の保存値が保たれる範囲を検査する。

## 検証

合成データと fake API だけで実コンポーネントの読み込み・描画・編集・保存を通し、入力欄の不在と更新 payload の既存値保持を確認する。i18n 検査・型検査・本番 build も実行する。

ブラウザでの配置・キーボード操作・Back/Forward と実機の保存往復は別の検証境界で、合成テストを通しても完了扱いにしない。本番ペルソナ、Pulse、LLM、永続データへは接触しない。
