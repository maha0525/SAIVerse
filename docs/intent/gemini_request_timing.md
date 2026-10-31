# Gemini の送信・応答時間の DEBUG 計測

**ステータス**: 検証待ち — 実装・隔離検証はブランチ `codex/gemini-http-timing`。develop 向け PR の確認と、取り込み後の通常利用ログ確認を待つ。

## 目的と範囲

音声・画像を含む会話が遅くなったとき、運用者が既存ログから「準備」「本文送信」「送信後の待ち」を分けて判断できるようにする。2026-10-31、まはーが DEBUG 出力と develop 向け PR を依頼した。過去の会話を再送する仕事や Files API への変更は含めない。

呼び出し元 → メディアの読み込み・変換 → キャッシュ判定 → Google SDK の直列化 → HTTP 本文送信 → 応答ヘッダー → 最初のチャンク・文字 → 既存の発話処理、という経路を対象とする。継続キャッシュの並行作成も別の呼び出しとして同じ HTTP 計測を通す。新しいログは通常の root handler から backend.log へ届き、ログ保存先・レベルは起動側が所有する。

## 不変条件

- 計測は DEBUG のときだけ行う。HTTP ライブラリ全体の DEBUG は有効にしない。
- 本文、音声、URL、query、認証情報、例外の本文を新しいログへ出さない。時刻、所要時間、送信サイズ、固定の段階名、ランダムな識別 ID と例外の型だけを記録する。
- 論理呼び出しの call_id と HTTP 試行ごとの request_id を分ける。並行するキャッシュ作成は parent_id で元の呼び出しへ結び付け、SDK 再試行・cache fallback の通信はそれぞれ異なる request_id になる。
- 接続・TLS、本文送信開始/完了、応答ヘッダー受信、失敗を、httpcore の trace callback で取得する。Content-Length を読み、本文を追加で走査・保存しない。
- 「本文送信完了」はクライアントが書き出しを終えた時点。Google が全文を受け取った時刻や純粋なモデル演算時間とは呼ばない。送信後の待ちには残りの通信も含む。
- リトライ、キャッシュの寿命・課金計上、生成結果、ストリームの close、接続設定を変えない。generator の yield をまたいで ContextVar を保持しない。

## 変更の所有者

`saiverse/gemini_clients.py` が SDK 共通の HTTP hook を登録する。`saiverse/gemini_timing.py` が識別子・単調時計・許可されたイベントだけの出力を所有する。`llm_clients/gemini.py` は変換完了・最初のチャンク/文字、`gemini_cache.py` はキャッシュ作成の論理境界を提供する。consumer が通信ログの全文から推測する構造にはしない。

## 検証

実 SDK と httpx/httpcore の経路を、合成レスポンスとメモリ内 network backend で通す。本文送信完了と応答ヘッダーの間に制御した時間差を置き、段階の順序・時刻・識別 ID・送信サイズと秘匿値の不在を確認する。並行キャッシュ、再試行、HTTP 失敗、ストリームを早く閉じる場合、INFO で計測を出さない場合も検査する。本番ペルソナや有料 API は呼ばない。

## ログの読み方

取り込み後はバックエンドを再起動し、通常の DEBUG 設定で使用する。新しい設定や追加の API 呼び出しは不要。保存先は従来の session の `backend.log`。

- `[gemini_timing]`: 論理呼び出しの `call_started`、`contents_ready`、`first_chunk`、`first_text`、`call_completed` / `call_failed` / `call_closed`。`elapsed_ms` は呼び出し開始からのミリ秒。
- `[gemini_http]`: SDK が直列化を終え、HTTPX の要求が用意できた `request_ready`、`connect_*`、`tls_*`、`headers_send_*`、`body_send_*`、`response_headers_*`。`*_started` / `*_complete` / `*_failed` で開始・終了・失敗を区別する。接続を再利用した場合は connect / tls は出ない。
- HTTP の `elapsed_ms` は `request_ready` から、`call_elapsed_ms` は対応する論理呼び出し開始からのミリ秒。`duration_ms` は同じ段階の開始からの所要時間。`body_bytes` は直列化済み HTTP 本文の Content-Length（base64 や JSON を含む）で、不明なら -1。
- 本文送信は `body_send_complete.duration_ms`。送信後から応答ヘッダーまでは、同じ `request_id` の `response_headers_complete.elapsed_ms - body_send_complete.elapsed_ms`。最初の文字までを見るときは同じ `call_id` の `first_text.elapsed_ms - body_send_complete.call_elapsed_ms` を使う。SDK がリトライした場合は最後に採用された試行を選び、それ以前の試行と再試行待ちは別に数える。
- 生成とキャッシュ作成は HTTP の `operation=generate_stream` / `generate` / `cache_create` で分ける。キャッシュの論理呼び出しは `operation=cache_create_auto` / `cache_create_sync` / `cache_create_bg`。その `parent_id` を生成の `call_id` と結び付ける。並行作成の時間を生成時間へ足さない。

HTTP/1.1 と HTTP/2 の trace に対応する。現在の Gemini 生成・キャッシュ作成が使う同期 HTTPX 経路が対象で、別クライアントの Live API / aiohttp 経路の計測へは広げない。ログの保存先や HTTP ライブラリのレベルは変更しない。

一次仕様: [httpcore trace](https://www.encode.io/httpcore/extensions/#trace)、[Google Gen AI SDK](https://googleapis.github.io/python-genai/)。

## 経緯

- 2026-10-31: 既存会話のログでは本文送信完了が欠けており、生成 API の最初の文字待ちとキャッシュ作成時間をアップロードと送信後の待ちへ分離できなかった。今後の記録を追加する依頼を受け、実装を開始。
- 2026-10-31: 実 SDK 2.21.0 / httpx 0.28.1 / httpcore をメモリ内通信へ接続し、本文送信125msと応答ヘッダー待ち750msを別々に検出。インライン音声のbase64を含む実送信サイズ、アプリの生成入口・自動キャッシュ入口、SDK再試行、失敗、並行呼び出し、INFO抑制、ストリームcloseを含む関連114件・26サブテストを通した。ruff、差分の空白検査、in_flight関所も合格。develop向けPRの確認・取り込み後は、通常利用で新しいDEBUG記録を確認する。
