# Issue: FastAPI の Content-Type 厳密検査を無効にしたまま (外部クライアント未監査)

**ステータス**: 🔲 未着手
**優先度**: high (2026-09-02 Codex レビューで引き上げ。下記「何が問題か」の 3 点目)
**作成日**: 2026-09-02
**関連**: `main.py` (`FastAPI(strict_content_type=False)`), `docs/intent/dependency_management.md` §3-1 (Web 一族の更新)

## 背景

FastAPI 0.132 から、リクエストボディを JSON として読むには `Content-Type: application/json` が付いていることが既定で必須になった (公式: Strict Content-Type。ブラウザが Content-Type 無しで送れることを突く CSRF への対策で、localhost で動かすアプリを特に想定している)。ヘッダが無い・違う場合、ボディは JSON として解釈されず、そのエンドポイントは 422 を返す。

2026-09-02 に Web 一族 (fastapi 0.116.1 → 0.141.1、starlette 0.47.3 → 1.6.0) を上げた際、SAIVerse にはブラウザ以外から JSON を POST するクライアントがあり、その全部がこのヘッダを正しく送っているかをその場で確かめられなかった。黙って 422 になる範囲を作らないため、`main.py` の `FastAPI(...)` に `strict_content_type=False` を渡して旧挙動 (ヘッダ無しでも JSON として読む) を保っている。

## 何が問題か

- 厳密検査は CSRF 対策なので、無効のままだと LAN 公開 (`--lan` / `OwnerAuthMiddleware`) 時の防御が一段薄い。
- 「監査が終わるまで」の暫定措置が、期限も対象一覧も無いと恒久化する。
- **loopback モード (既定の起動) では `OwnerAuthMiddleware` が付かず、CORS は cross-origin の単純 POST 自体を止めない** (2026-09-02 Codex レビュー指摘)。つまりブラウザで開いた悪意あるページから `http://127.0.0.1:8000/api/...` へ Content-Type 無しの JSON を投げる CSRF に対して、この旗が唯一の防御になりうる。ただし **0.116 時代には検査そのものが無かった**ので、この旗で以前より弱くなったわけではなく、「新しく得られる防御を保留している」状態。だからこそ保留は短くする。

## 監査対象に足すもの (2026-09-02 追記)

- **フロントエンド (`frontend/`) 自身**: `fetch` に文字列ボディを渡すとき `Content-Type: application/json` は自動では付かない。API ヘルパーが全経路でヘッダを付けているかを先に確かめる — ここが漏れていると旗を外した瞬間に UI 全体が 422 になる。
- 監査で判明した事実 (2026-09-02): stackchan `speak_hook.py:456` の POST は PCM 音声 (`data=` の iterator) で JSON ではないため対象外。Discord ゲートウェイ `auth.py:117` の POST は Discord のトークン URL 宛てで本体 API 宛てではないため対象外。残りは Godot アドオン・stackchan ファームウェア・test_fixtures・フロントエンド。

## 監査対象 (全部が `application/json` を送っていると確認できたら旗を外す)

| クライアント | 場所 | 確認すること |
|---|---|---|
| Godot vessel アドオン | `expansion_data/saiverse-godot-vessel-addon/sai-vr/addons/godot_ai/plugin.gd` と同 `utils/update_manager.gd` | `HTTPRequest` / `HTTPClient` の request に渡す headers に `Content-Type: application/json` があるか |
| stackchan アドオン (本体側) | `expansion_data/saiverse-stackchan-addon/` の `speak_hook.py` | `requests` / `httpx` / `urllib` で JSON を送る箇所が `json=` (自動でヘッダが付く) か、手組みの `data=` (付かない) か |
| stackchan ファームウェア | 同アドオン配下の firmware (ESP32 側から本体 API を叩く箇所) | HTTP クライアントに Content-Type を明示しているか |
| Discord ゲートウェイ | `discord_gateway/bot/auth.py` | 本体 API への POST のヘッダ |
| テスト用クライアント | `test_fixtures/test_api.py` | `requests.post(..., json=...)` になっているか (`data=json.dumps(...)` はヘッダが付かない) |

上の表に無いクライアント (curl の手打ち、ユーザー自作スクリプト) は監査の対象外。旗を外したあとに 422 が出たら、送る側でヘッダを付けるのが正しい修正で、本体側で再び緩めない。

## 完了条件

1. 表の 5 箇所すべてが `Content-Type: application/json` を送っていることを、コードを読んで (可能なら実機で一回ずつ) 確認した。
2. `main.py` から `strict_content_type=False` とそのコメントを削除した。
3. 隔離環境 (`docs/test_environment.md`) で `python test_fixtures/test_api.py --quick` が通る。
4. この issue を `docs/issues/archive/` へ移した。
