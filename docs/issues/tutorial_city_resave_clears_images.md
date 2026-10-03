# Issue: チュートリアルで City を保存し直すと既存の画像設定が消える

**ステータス**: 🔲 未着手（原因のソース照合済み。修正方式の選択・実装・隔離往復検証は未実施）
**優先度**: high
**作成日**: 2026-10-03
**関連**: [ワールドエディタ設定棚卸し](world_editor_settings_sync.md) / [City の識別子と表示名](../intent/city_identity.md)

## 症状と影響

画像を設定済みの既存 City で TutorialWizard の City 設定保存を通ると、
利用者が画像を編集していなくても `HOST_AVATAR_IMAGE` と `MAP_BACKGROUND_IMAGE` が
`None` になる。街マップの背景と案内役のアバターへの設定参照が失われる。
画像ファイル自体の削除を示す処理は、この更新経路にはない。

## 原因の経路（調査版 `53944ed9faf3eecd03e92ea5a07ec1b1329479a2`）

1. [`TutorialWizard.saveCityName`](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/tutorial/TutorialWizard.tsx#L296-L323)
   は既存 City を取得し、表示名・説明・オンライン設定・ポート・タイムゾーン・言語を PUT する。
   **`host_avatar_path` と `map_background_image` は送らない**。
2. [`CityUpdate` と PUT の受け渡し](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L42-L52)
   は未送信の2フィールドを `None` にし、[更新ルート](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L205-L207)
   がその値を manager へ渡す。未送信と明示的な解除が区別されない。
3. [`AdminService.update_city`](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/manager/admin.py#L290-L303)
   は `HOST_AVATAR_IMAGE` と `MAP_BACKGROUND_IMAGE` をともに `None` で上書きして commit する。

## 修正を選ぶときの条件

画面の入力 → 更新 API → DB → 再表示までで、**編集していない設定は保持し、明示した解除だけを反映する**。

- 送信側で既存の画像値を再送する案: 現在の全項目 PUT 契約を保つ小さい修正。
  ただし取得後の別画面の変更を古い値で戻す窓は残るので、他の全項目保存経路も確認する。
- 更新 API で未送信項目を保持する案: 保存の持ち主で全 caller に共通の区別を与える。
  明示的な `null` / 空文字による解除と未送信を分け、既存 caller の互換を確かめる必要がある。

この issue は方式を決定せず、ランタイムも変更していない。City の内部識別子の不変条件は維持する。

## 検証の出口

隔離 DB の合成 City に2画像を設定し、チュートリアルと同じ payload → 実 API / manager → DB の再読込まで通す。
画像未送信時の保持、明示解除、片方だけ変更、初回の画像なし City を確認する。
さらに隔離 UI で再保存 → CityMap / 案内役の再表示を確認する。本番 City は使わない。

## 記録

- 2026-10-03: [PR #362 のレビュー](https://github.com/maha0525/SAIVerse/pull/362#issuecomment-5964788305) の指摘を、上記3境界のソースで確認して起票。これはソース照合の記録であり、この文書追加で修正・実機検証が完了したとは扱わない。
