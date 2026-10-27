# GPT-6.1 Terra（Codex / 従量 API 接続）

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

## 従量 API 接続

設定キー `gpt-6.1-terra` は `openai` プロバイダーの既存 Chat Completions 経路を使う。
通常会話・画像入力・構造化出力に対応し、本文の Spell はネイティブツールに依存しない。
**Chat Completions 経由なので、`available_tools` を使う Playbook ではこのモデルの
ツール呼び出しは動かない。** これは設定で拒否するガードではなく利用上の制約であり、
その用途にはこの設定を選ばないこと。Responses 対応の実装変更は含まない。

- API の正式 ID は同じ `gpt-6.1-terra`。API キーと利用権限が別途必要。
- 同世代の設定と同じく表示名は `GPT-6.1 Terra (< 272k)`、文脈上限は 272000。
  これは通常料金帯に収めるためのローカル設定で、公式最大値ではない。
- 標準料金は 100 万トークンあたり入力 $1、キャッシュ入力 $0.05、出力 $5。
  入力が 272000 を超える場合はリクエスト全体で入力・キャッシュ入力が 2 倍、
  出力が 1.5 倍になるため、既存の長文脈料金フィールドにも記録する。
  費用表示は推定であり、キャッシュ書込料金・保持時間、Batch/Flex、Fast、地域加算などの
  別条件を網羅する請求額ではない。
- `reasoning_effort` は `none / low / medium / high / xhigh / max`、既定は `medium`。
- `max_completion_tokens` は既存同世代と同じ 8192 をローカル上限・既定にする。
  公式最大出力 128000 をそのまま許可する変更ではない。
- `verbosity` の設定形は同世代に合わせるが、`client_support: ["responses"]` とする。
  現行 Chat Completions クライアントの許可リストには含まれず送られない。温度・top_p・logprobs 等は追加しない。
- 呼出側が温度を直接指定する既存経路（Chronicle 軽量生成の `temperature=0.3` 等）は、
  `openai` プロバイダの `supports_sampling_with_reasoning: false` により、推論あり
  （`reasoning_effort` が `none` 以外）のとき送信直前で温度・top_p・logprobs 等を除く
  （PR #427、`docs/reference/providers.md`）。通常経路の既定温度は未指定。

## 根拠と検証境界

2026-10-27 確認:

- [公式発表](https://openai.com/index/introducing-gpt-6-1-terra/)
- [Codex のモデル一覧](https://learn.chatgpt.com/docs/models): 正式 ID と Codex 提供。
  そこに記載の CLI 0.159.0 以降という要件は CLI 向けで、
  SAIVerse の既存 HTTP クライアントが CLI を起動するという意味ではない。
- [API モデル資料](https://developers.openai.com/api/docs/models/gpt-6.1-terra):
  画像入力・構造化出力、料金・上限・推論設定、ツールに限る Responses 要件。
- [GPT-6 移行ガイド](https://developers.openai.com/api/docs/guides/latest-model):
  Chat Completions の対応、`reasoning_effort` と非対応サンプリングパラメーター。

オフライン回帰で、同梱 JSON → provider 解決 → 実クライアントのリクエスト本文
（Codex の画像・ツール・構造化出力、従量 API の画像・構造化出力・パラメーター）までを検査する。認証ストア・本番ペルソナ・ネットワークには
触れない。実応答、実アカウントの提供可否、最大文脈、モデル固有の品質は未検証。
