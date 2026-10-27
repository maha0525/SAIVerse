# GPT-6.1 Terra（Codex 接続）

既存の [モデル管理](../intent/model_provider_management.md) と
[Codex 接続](../intent/codex_subscription_auth.md) に、選択肢だけを追加する。
既定モデル・保存済み設定・認証・プロバイダー実装は変えない。

- 設定キー: `codex-gpt-6.1-terra`。正式モデル ID: `gpt-6.1-terra`。
- 接続: `openai_codex` の既存 Responses 経路。画像入力、ツール、構造化出力の
  既存変換を使用する。利用可否はアカウント・プラン・提供状況に依存する。
- `context_length: 272000` は既存 Codex 設定に合わせた **SAIVerse 側の保守的な上限**。
  公式 API 資料の最大値（1,050,000 文脈 / 128,000 出力）とは別で、
  この接続で最大長を実測した値ではない。
- `parameters` は空。推論 effort や出力上限を新たに固定しない。
  このクライアントが送らないサンプリングパラメーターを追加しない。
- サブスクリプション経由のため、従量 API の料金を転記しない。
  使用量画面の費用 0 は「無料・無制限」の意味ではない。
- phase ごとのスペル重複を避ける既存の説明を維持する。

## 従量 API 版を追加しない理由

公式資料では Terra 6.1 のツール呼び出しは Responses API が必要。
SAIVerse の `openai` プロバイダーは Chat Completions を使うため、
同じモデル ID の JSON を追加するだけではツールが動く保証がない。
従量 API 版の Responses 対応は、この設定追加から分離した実装・設計判断とする。

## 根拠と検証境界

2026-10-27 確認:

- [公式発表](https://openai.com/index/introducing-gpt-6-1-terra/)
- [Codex のモデル一覧](https://learn.chatgpt.com/docs/models): 正式 ID と Codex 提供。
  そこに記載の CLI 0.159.0 以降という要件は CLI 向けで、
  SAIVerse の既存 HTTP クライアントが CLI を起動するという意味ではない。
- [API モデル資料](https://developers.openai.com/api/docs/models/gpt-6.1-terra):
  画像入力・ツール・構造化出力と上限、Responses 要件。

オフライン回帰で、同梱 JSON → provider 解決 → 実クライアントのリクエスト本文
（画像・ツール・構造化出力）までを検査する。認証ストア・本番ペルソナ・ネットワークには
触れない。実応答、実アカウントの提供可否、最大文脈、モデル固有の品質は未検証。
