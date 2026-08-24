# ユーザー会話レーンの失敗が UI に何も出ない (安全装置の発火が「無言の無応答」になる)

**ステータス**: 未解決 (実害確認済み — 2026-08-24、まはーが実機で遭遇)
**作成日**: 2026-08-24
**関連**: `saiverse/user_conversation.py` / `sea/pulse_controller.py` / `saiverse/provider_security.py` / provider 保存系 API

## 現物 (2026-08-24 の実例)

NEBULA (LAN 上の別機体) の llama-swap を指す provider (`base_url: http://192.168.0.221:8092/v1`) を UI から選んでペルソナに話しかけたところ、**返事が来ないだけで、UI には何も表示されなかった**。実際には `provider_security.validate_provider_url` が「平文 HTTP の外部ホストは `SAIVERSE_PROVIDER_ALLOWED_HOSTS` に載せない限り拒否」で正しく発火していて、その `ValueError` → `LLMError` が `_start_main_line_pulse` まで上がり、ログにだけ書かれて消えていた。

安全装置の判定そのものは正しい (検査は使う瞬間 = 境界にあり、これは動かさない)。問題は**止めた事実と直し方が本人に届かない**こと。一般ユーザーは環境変数の存在を知り得ないので、「壊れた」以外の解釈ができない。

## 穴は三段ある

1. **ユーザー会話レーンにエラー通知が無い。** メタ判断レーンには `PulseController._notify_meta_error` があり、失敗を `event_callback({"type": "error", ...})` で UI へ流す (`sea/pulse_controller.py:571`)。ユーザーレーン (`submit_user` → `submit` → `_do_execute`) には同じものが無く、例外は `saiverse/user_conversation.py` の `_start_main_line_pulse` 末尾の `except Exception: LOGGER.exception(...)` (2026-08-24 時点で 761 行付近) が握り潰して終わる。すぐ上に「event_callback が無いとイベントは虚空へ流れ、フロントに吹き出しが出ない」というコメントがあるのに、エラーだけが虚空行きのまま。
   - 同族の疑い: メタレーンの `except LLMError: raise` (`pulse_controller.py:557`) は **LLMError のときだけ `_notify_meta_error` を飛ばして**再送出している。意図的な非対称かは未調査 — 直すときに要確認 (規律: 直した欠陥の同族を検算する)。
2. **例外の文言が開発者語。** "Plain HTTP provider URLs require loopback or an explicit allowed host" は原因も直し方も一般ユーザーに伝えない。ユーザー向け文言は「何を止めたか + なぜ + どうすれば許可できるか」の三点セットにする。
3. **検査の発火が登録時に無い。** provider を UI で保存した時点では検査されず、最初の会話で初めて落ちる。境界の検査は残した上で、保存 API でも `validate_provider_url` を呼び、その場で警告を返す (入口の検査は境界の保証ではないが、UX としては要る)。

## 修正案 (起票時点の見立て)

1. `_start_main_line_pulse` の catch で、手元の `event_callback` があれば `{"type": "error", "message": <ユーザー向け文言>}` を流してからログする (メタレーンの `_notify_meta_error` と同じ型)。フロントエンドが `type: "error"` イベントをどう描画するかの確認と、描画されない場合はその実装も含む。
2. `provider_security.py` の各 `ValueError` 文言をユーザー向け (日本語、直し方付き) に改める。
3. provider 保存系 API に登録時検査を足す。

## 保留の理由

`user_conversation.py` は feature/autonomous-behavior-v2 の v3 未コミット変更を抱えており、混ぜると v3 の検収が濁る (2026-08-24 まはー裁定: 起票して、着手タイミングは後で決める)。

## 回避策 (それまでの運転)

平文 HTTP で LAN 上の推論サーバーを使う場合は `.env` に `SAIVERSE_PROVIDER_ALLOWED_HOSTS=<ホスト>` (カンマ区切り) を書いて再起動する。2026-08-24 に `192.168.0.221` (NEBULA) を追記済み。
