# NVIDIA NIM の structured output が モデルの `request_kwargs` を落とす

**状態**: 解決済み (2026-09-11)。2026-08-04 起票、OpenRouter アプリ帰属ヘッダーの Codex レビュー二巡目で発見した、帰属ヘッダーとは独立した既存の欠陥。

**解決の中身 (2026-09-11)**: 組み込みモデルの棚卸しで `nim-deepseek-v4-flash-0731` (thinking を `extra_body` で指定) をチュートリアルの NIM 設定の軽量モデルに選んだところ、Codex レビューがこの欠陥を high で指摘したので直した。生 HTTP 経路も SDK 経路と同じ `build_request_kwargs` で組み立て、`openai_runtime.split_sdk_request_options` で SDK と同じようにボディと SDK 専用の指定に分ける。`extra_body` はボディの最上位へ、`extra_headers` はヘッダーへ、`extra_query` は URL へ写す。構造化出力が持ち主のキー (`model` / `messages` / `n` / `tools` / `tool_choice`) だけは、設定から上書きできない (上書きしようとした設定はキー名を WARNING に出す)。検出の穴だった「factory を通していない」は、同梱の NIM 定義を factory 経由で組み立てて送信内容を読むテスト (`tests/test_nim_structured_output_request.py`) で塞いだ。隔離環境から SAIVerse のクライアントで実 API へ構造化出力を送り、thinking の指定つきで受け付けられることも確かめた。なお下の本文の「`extra_headers` は捨てられる」は起票後に先に直っていて、今回の時点ではヘッダーには写っていた。

関連: [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md) §9「モデル固有の API 契約をモデル定義からプロバイダ境界まで保つ」

## 芯

**同じモデルが、structured output を頼まれたときだけ別の設定で動く。**

NIM は Mistral 系が `guided_json` / `response_format` に対応しないため、`llm_clients/nvidia_nim.py` の `_create_nim_structured_output_via_tool()` が SDK を通らず生の HTTP でリクエストを組み立てる。この経路が body へ写しているのは `temperature` / `top_p` / `max_tokens` の3つだけで、モデル JSON の `request_kwargs` にある **`extra_body` と `extra_headers` は捨てられる**。

## 実害

同梱モデルのうち、`request_kwargs.extra_body` を持つ NIM モデルは 3 枚（2026-09-11 時点）。

```
nim-kimi-k2.6 / nim-deepseek-v4-pro-0813 / nim-deepseek-v4-flash-0731
```

2026-08-04 の起票時点では 8 枚あったが、そのうち 7 枚は NIM 側で提供が終了したため、2026-09-11 に同梱から削除した。

いずれも `extra_body.chat_template_kwargs` で thinking の有無を指定している。キーの名前はモデルによって違う (Kimi K2.6 は `enable_thinking`、DeepSeek V4 の 0813 / 0731 版は `thinking`)。例 (`nim-deepseek-v4-flash-0731.json`):

```json
"request_kwargs": {
  "extra_body": {
    "chat_template_kwargs": { "thinking": true }
  }
}
```

通常の会話では thinking が有効になり、structured output を要求した瞬間に無効な状態で走る。**モデル定義が capability の正典である**という §9 の責任境界が、この経路でだけ破れている。エラーにはならないので、出力の質が変わったことにしか現れない。

## 対応の方向

**SDK 経路と raw 経路で body / header の組み立てを共有する**のが芯。今は「SDK に渡す組み立て」と「手で書く組み立て」が別々に存在し、片方を直しても他方が追随しない構造になっている。`llm_clients/openai_runtime.py: build_request_kwargs` が前者を担っているので、raw 経路もそこを通せるかを検討する。

予約ヘッダー (`Authorization` 等) の扱いだけは例外で、クライアントが所有し設定に上書きさせない (`llm_clients/openai.py: _strip_reserved_headers`)。共有する際もこの境界は維持する。

## 検出できなかった理由

`tests/test_llm_clients.py` の NIM テストは `NvidiaNIMClient` を直接構築しており、**factory を通して実際のモデル設定を解決していない**。`request_kwargs` を持たないクライアントを検証しているため、この欠落は構造上テストに映らない。修正時は同梱の NIM モデル設定を factory 経由で通すテストを併せて用意する。
