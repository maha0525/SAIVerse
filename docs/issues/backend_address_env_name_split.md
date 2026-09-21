# Issue: 画面がバックエンドの居場所を 2 つの別名で読んでいて、隔離テストでもアドオン通信だけ本番へ行く

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-09-21
**関連**: `frontend/next.config.ts` / `frontend/src/app/api/**/route.ts` 3 本 / `test_fixtures/start_test_frontend.bat`

## 背景

### 何が起きるか

隔離テスト環境の手順どおりに起動しても、画面から出ていくアドオン関係の通信だけが、隔離したバックエンド (18000) ではなく **本番のバックエンド (8000)** を向く。

向き先が本番になるのは、住所が `/api/addon/...` と `/api/mcp/...` で始まる通信すべて。中身で言うと次の 4 つ。

- アドオンの一覧取得と、有効・無効の切り替え
- アドオンの設定の読み書き (ペルソナごとの設定を含む)
- アドオンからの知らせの受信 (音声が用意できた、など)
- アドオンが配る音声ファイルの取得

**読み取りだけではない。** 有効・無効の切り替えや設定の保存も同じ道を通るので、隔離環境でアドオンの設定をいじったつもりが、本番のアドオン設定を書き換えることになる。画面には何の警告も出ない。

### なぜそうなるか

画面は「バックエンドの居場所」を、**2 つの別々の名前**で読んでいる。

| 名前 | 読んでいる場所 | 何の行き先になるか |
|---|---|---|
| `SAIVERSE_BACKEND_ORIGIN` | `frontend/next.config.ts` | 上に挙げた以外の、普通の通信すべて |
| `SAIVERSE_BACKEND_URL` | `frontend/src/app/api/addon/events/route.ts`<br>`frontend/src/app/api/addon/[...path]/route.ts`<br>`frontend/src/app/api/mcp/[...path]/route.ts` | アドオンと MCP の通信 |

隔離テストの画面起動ファイル `test_fixtures/start_test_frontend.bat` は、**前者しか設定しない**。後者は、設定されていないときは `"http://127.0.0.1:8000"` (= 本番) を使う、という既定値を持っている。

どちらの名前も `.env.example` にも `docs/reference/environment-vars.md` にも載っていない。

### 名前が 2 つある経緯

アドオン用の中継は後から別に作られた。普通の中継の仕組み (Next.js の rewrites) では、音声ファイルの分割取得 (`Range` ヘッダと 206 応答) と、知らせを受け続ける長い接続 (SSE) が正しく流れなかったため。各ファイルの冒頭コメントにその理由が書いてある。名前が割れたのはその時の副作用と見られる。

### 確かめた範囲

コードを読んで確認した事実は 3 つ。名前が 2 つに割れていること、隔離テストの画面起動ファイルが片方しか設定しないこと、設定されていないときの既定値が本番を指していること。

**確かめていないこと**: 本番バックエンドを立てた状態で隔離環境の画面を開き、実際に本番へ通信が飛ぶのを観測する実験はしていない。2026-09-21 の検証では、両方の名前を自分で設定して回避した。

### この割れ自体は既知

2026-09-10 と 2026-09-12 の監査に「`SAIVERSE_BACKEND_ORIGIN` と `SAIVERSE_BACKEND_URL` に割れており、どちらも `.env.example` にも環境変数リファレンスにも無い」と記録がある。**隔離テストが漏れる**ところまで結びつけた記録は、この issue が初出。

## 解決案候補

### A. 隔離テストの起動ファイルに 1 行足す

`test_fixtures/start_test_frontend.bat` に `SAIVERSE_BACKEND_URL` の設定を足す。この起動ファイルに `.sh` 版は無いので、直すのは 1 ファイルだけ。

すぐ終わるが、名前の割れは残る。隔離テストの穴は塞がっても、別の起動のしかた (タネット越し、LAN、別ホスト) を用意する人が同じ穴に落ちる。

### B. 名前を 1 つに統一する (推奨)

画面側の 4 箇所が同じ名前を読むようにして、`.env.example` と環境変数リファレンスに載せる。起動ファイル側には何も足さなくてよくなり、監査で挙がっている宿題も同時に片付く。

移行の注意: すでに `SAIVERSE_BACKEND_URL` を設定して運用している人がいると、統一した瞬間に動かなくなる。当面は旧名も読む (新しい名前が無ければ旧名を使う) 形にして、リファレンスに「旧名は非推奨」と書き添えるのが安全。

## 関連リソース

- `frontend/next.config.ts` — 普通の通信の中継先を決めている
- `frontend/src/app/api/addon/events/route.ts` — アドオンの知らせの中継 (別名を読む)
- `frontend/src/app/api/addon/[...path]/route.ts` — アドオン通信の中継 (別名を読む)
- `frontend/src/app/api/mcp/[...path]/route.ts` — MCP 通信の中継 (別名を読む)
- `test_fixtures/start_test_frontend.bat` — 片方しか設定していない起動ファイル
- [`docs/test_environment.md`](../test_environment.md) — 隔離テスト環境の手順
- `docs/audits/2026-09-10_normal_behavior_remaining_evidence/findings.md` (2355 行目・2482 行目付近)
- `docs/audits/2026-09-12_normal_behavior_consolidation/normal_behavior.md` (1230 行目付近)

## ログ

- 2026-09-21: 起票。アドオンのクライアント操作がメタデータを読めない不具合 ([PR #313](https://github.com/maha0525/SAIVerse/pull/313)) を隔離環境で検証している最中に踏んだ。A / B のどちらで直すかは未決。
