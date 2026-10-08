# Intent: 画像生成モデルの追加

**ステータス**: 検証待ち。Nano Banana 2.1 の追加と隔離 mock 回帰は完了。実 API・本番ペルソナの検証は対象外。

## 全体と不変条件

利用者・ペルソナが Playbook または `generate_image` ツールで指定した画像モデルを、
対応するプロバイダへ渡し、返された画像を従来のメディア保存・表示経路へ届ける。
モデル選択の正典は画像ツールであり、会話用モデル設定とは独立する。
ツールの型・利用可能一覧・スキーマ・Playbook の選択肢は新モデルを同じ名前で公開する。
既存のモデル ID と品質設定は変更しない。まはー確認済みのレビュー方針により、
既定モデルと自動フォールバックの Nano Banana 2 の位置は 2.1 へ置き換える。

## Nano Banana 2.1

- 選択名 `nano_banana_2_1` を追加し、公式 ID `gemini-nano-banana-2.1` へ送る。
- 既存 Nano Banana 2 のリクエスト・応答処理を再利用し、有料の `GEMINI_API_KEY` を必要とする。
- 品質 low/medium/high は既存契約どおり 1K/2K/4K。xhigh/max は 4K。
  API 自体の既定 1K とは別に、ツールは従来の全体品質設定を適用する。
- テキストと画像の応答、参照画像（プロバイダ上限14枚）、縦横比を扱う。
  function calling・structured output・cache は送らない。
- 既定値と自動 fallback の先頭を `nano_banana_2_1` にする。旧 `nano_banana_2` は明示指定の互換経路として残す。
  旧 preview API ID の stable ID 移行は別作業で、この変更には含めない。

公式仕様（2026-10-08 確認）:
https://ai.google.dev/gemini-api/docs/models/gemini-nano-banana-2.1

## 検証の境界

隔離テストでスキーマ・Playbook から選択可能なこと、選択名から SDK の正式 ID・
品質・参照画像・応答形式へ届くこと、画像 bytes/MIME が既存保存経路へ渡ること、
有料キー必須、旧モデルの明示指定、2.1 が既定値・fallback の先頭になることを確認する。
SDK 呼び出しは mock とし、実 API の生成品質・課金・本番保存は確認しない。
