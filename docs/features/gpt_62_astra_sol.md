# GPT-6.2 Astra / Sol（Codex / 従量 API 接続）

既存の [モデル管理](../intent/model_provider_management.md) と
[Codex 接続](../intent/codex_subscription_auth.md) に、選択肢だけを追加する。
既定モデル・保存済み設定・認証・プロバイダー実装は変えない。

## Codex 接続（ChatGPT サブスクリプション）

- 設定キー: `codex-gpt-6.2-astra` / `codex-gpt-6.2-sol`。
  正式モデル ID: `gpt-6.2-astra` / `gpt-6.2-sol`。
- 接続: `openai_codex` の既存 Responses 経路。画像入力、ツール、構造化出力の
  既存変換を使用する。利用可否はアカウント・プラン・提供状況に依存する。
- `context_length: 272000` は既存 Codex 設定（`codex-gpt-6.1-sol` / `codex-gpt-6.1-terra`）に
  合わせた **SAIVerse 側の保守的な上限**。公式 API 資料の最大値（1,050,000 文脈 / 128,000 出力）とは別で、
  この接続で最大長を実測した値ではない。
- `parameters` は空。推論 effort や出力上限を新たに固定しない。
  このクライアントが送らないサンプリングパラメーターを追加しない。
- サブスクリプション経由のため、従量 API の料金を転記しない。
  使用量画面の費用 0 は「無料・無制限」の意味ではない。
- phase ごとのスペル重複を避ける既存の説明を維持する。

## 従量 API 接続

設定キー `gpt-6.2-astra` / `gpt-6.2-sol` は `openai` プロバイダーの既存 Chat Completions 経路を使う。
通常会話・画像入力・構造化出力に対応し、本文の Spell はネイティブツールに依存しない。
**Chat Completions 経由なので、`available_tools` を使う Playbook ではこのモデルの
ツール呼び出しは動かない（OpenAI 公式仕様上、両モデルのツール呼び出しには Responses API が必要）。**
これは設定で拒否するガードではなく利用上の制約であり、その用途には Codex 版または他モデルを選ぶこと。
Responses 対応の実装変更は含まない。

- API の正式 ID は同じ `gpt-6.2-astra` / `gpt-6.2-sol`。API キーと利用権限が別途必要。
- 同世代の設定と同じく表示名は `GPT-6.2 Astra (< 272k)` / `GPT-6.2 Sol (< 272k)`、文脈上限は `272000`。
  これは通常料金帯に収めるためのローカル設定で、公式最大値ではない。
- 標準料金（100 万トークンあたり）:
  - `gpt-6.2-astra`: 入力 `$8.00`、キャッシュ入力 `$0.40`、出力 `$40.00`
  - `gpt-6.2-sol`: 入力 `$2.00`、キャッシュ入力 `$0.10`、出力 `$10.00`
  - 入力が `272000` を超える場合はリクエスト全体で入力・キャッシュ入力が 2 倍、出力が 1.5 倍になるため、
    既存の長文脈料金フィールドにも記録する。費用表示は推定であり、キャッシュ書込料金・保持時間、
    Batch/Flex、Fast、地域加算などの別条件を網羅する請求額ではない。
- `reasoning_effort` は `none` / `minimal` 非対応のため `low / medium / high / xhigh / max`、既定は `medium`。
- `max_completion_tokens` は既存同世代と同じ `8192` をローカル上限・既定にする。
  公式最大出力 `128000` をそのまま許可する変更ではない。
- `verbosity` の設定形は同世代に合わせるが、`client_support: ["responses"]` とする。
  現行 Chat Completions クライアントの許可リストには含まれず送られない。温度・top_p・logprobs 等は追加しない。
- 呼出側が温度を直接指定する既存経路は、`openai` プロバイダの `supports_sampling_with_reasoning: false` により、
  推論ありのとき送信直前で温度・top_p・logprobs 等を除く（`docs/reference/providers.md`）。通常経路の既定温度は未指定。

## 根拠と検証境界

2026-11-18 確認:

- API モデル ID・コンテキスト長（1,050,000 / 最大入力 922,000 / 最大出力 128,000）・料金・272K 超長文割増・
  `reasoning_effort`（`none`/`minimal` 非対応、推奨既定 `medium`）・ツール呼び出しの Responses API 要件を公式資料で確認。
- オフライン回帰（`tests/test_gpt_62_models.py`, `tests/test_model_configs.py`, `tests/test_model_picker.py`）で、
  同梱 JSON → provider 解決 → 料金計算 → Codex クライアントのリクエスト組み立てまでを検査する。
  実 API / 実 Codex サーバー・認証ストア・本番ペルソナ・ネットワークには触れない。
