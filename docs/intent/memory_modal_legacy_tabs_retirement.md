# Intent: メモリーモーダル旧点検タブの退役

**ステータス**: ✅ 完了 (2026-07-16)

## 1. 目的

メモリーモーダルから「7層ストレージ」と「Tracks」を退役する。

両タブは、認知モデル実装初期の内部状態を直接点検するためのデバッグ面だった。その後、日常利用で知りたい情報にはそれぞれ正規の観察面ができた。

- いま何をしているか・最近どう動いたか: ライフビュー
- 暮らしの軸・大切にしていること・最近の取り組み: Profile
- Pulse 内の思考・入力プロンプト・メッセージ: Pulse タイムライン
- 保存された会話・記憶本文: チャットログ

メモリーモーダルは記憶本文と Pulse の深掘りに集中させ、旧認知モデルの構造をユーザー導線に残さない。

## 2. 退役前の情報監査

完全な同一表示への置換ではない。旧タブ固有だったものを次のように分類する。

| 旧タブ固有の表示・操作 | 判定 | 退役後の扱い |
|---|---|---|
| 全 Track の内部 status、forgotten、raw metadata、内部 ID、各種 timestamp | 実装検証情報。日常の自己像・活動観察には不要 | API `GET /api/people/{id}/tracks` を保守用に残す |
| Track 個別の pause / activate | 検証用操作。正規のユーザー操作はライフビューの自律 ON/OFF | API と `scripts/debug_track.py` を保守用に残す |
| main/sub の保存メッセージ、line role、scope | Pulse タイムライン／チャットログと重複 | 正規画面を利用 |
| meta judgment の trigger context、commit flag、raw prompt snapshot | 内部診断情報。ライフビューは判断結果を日常語で表示し、Pulse タイムラインは入出力を表示 | `storage-layers` API を保守用に残す |
| track local log | 現行コードに書き込み元がなく、旧データの診断用途のみ | API を保守用に残す |
| 7層ごとの件数 | 実装構造の点検値。第4層は揮発で常に0、第7層は未実装、第6層はチャットログへの案内だけ | UI から退役 |
| meta judgment / track local の旧汚染ログ削除 | 過去の修復用操作 | DELETE API を保守用に残す |

したがって、失われるのはユーザー向け情報ではなく、内部デバッグ情報の常設 UI だけである。診断経路そのものは残す。

## 3. 変更範囲

- `MemoryModal` のタブ型、ボタン、描画分岐、専用 import から2タブを削除する。
- `StorageLayersViewer` / `TracksViewer` と専用 CSS を削除する。
- 現行仕様を説明する文書から、両画面を利用可能な現役 UI として扱う記述を外す。
- backend の Track / storage-layers API、DB テーブル、runtime の書き込み経路には触れない。

## 4. 完了条件

- メモリーモーダルに「7層ストレージ」「Tracks」が表示されない。
- 削除したコンポーネントへの参照が残らない。
- TypeScript 検査、lint、production build が通る。
- Pulse タイムライン、Profile、ライフビューの導線は変わらない。

## 5. 実装・検証記録

- `MemoryModal.tsx` から2タブの import・タブ定義・ボタン・描画分岐を削除。
- `StorageLayersViewer` / `TracksViewer` と専用 CSS 4ファイルを削除。
- frontend 内に削除コンポーネントへの参照がないことを `rg` で確認。
- `tsc --noEmit`: 成功。
- ESLint: 0 errors（既存 warnings 243件。今回変更ファイルへの指摘なし）。
- Next.js production build: 成功。
