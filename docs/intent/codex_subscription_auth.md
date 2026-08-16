# Intent: Codex サブスク認証の自立 — SAIVerse 自身でログインする

**ステータス**: 完了 (v0.2, 2026-08-16)。全層実装済み・Codex レビュー **8 巡で approve** 到達 (競合系の消し込み全経緯は [ハンドオフ](../handoff/2026-08-16_codex_subscription_auth_handoff.md) §8)。テスト 44 本 + フルスイート 4455 緑。**実機で end-to-end 確認済み**: SAIVerse 自身のログイン → 自前ストアへのトークン保存 → そのトークンで Codex サブスク経由の発話まで成功 (2026-08-16、まはー)。実機一発目で踏んだ認証ホストの curl_cffi 地雷 (§クライアントの使い分け) も修正・再確認済み。
**未検証の境界 (完了扱いだが実機未確認、実害小)**: ①ログアウトが `~/.codex` を消さないこと (単体テスト済み・実機未確認) ②同一 client_id での SAIVerse ログインと Codex CLI ログインの共存 (§6、推測のまま) ③§6「lease 返却不達の族」の受容はまはー裁定を待つ残存リスク (実装対応はせず記録のみ)。いずれも通常利用の中で確認可能で、機能の完了を妨げない。
**経緯**: Hermes Agent の実装読解 (2026-08-16) で OpenAI のデバイスコード方式が判明し、まはー裁定で同日着手。消し込みは当初 Opus 予定をまはー裁定で同セッション続投。
**関連**: [`model_provider_management.md`](model_provider_management.md)（プロバイダ管理の全体像。openai_codex は builtin 固定プロバイダ） / [`codex_window_addon.md`](codex_window_addon.md)（Codex CLI をラップする別機構。あちらは CLI 前提のまま＝本書のスコープ外）

---

## 1. これは何か

**SAIVerse が自分で ChatGPT アカウントにログインし、Codex サブスク経由の発話に使うトークンを自前で持てるようにする。**

これまで Codex サブスクで発話するには「Codex CLI をインストール → `codex login` → config.toml の保存形式を書き換え」という前置きが必要で、SAIVerse は CLI が作る `~/.codex/auth.json` に相乗りしていた。本件後は、設定画面の「ログイン」ボタン → ブラウザで OpenAI のページを開いてコードを入力 → 完了、になる。Codex CLI は不要になる（ただし既にある相乗りも壊さない）。

方式は **デバイスコードログイン**（テレビの YouTube にスマホでログインするのと同じ流れ）。Hermes Agent（NousResearch）が実働させているのを読解して確認済み:

1. `auth.openai.com/api/accounts/deviceauth/usercode` に client_id を送ると、短いコード（user_code）が返る
2. ユーザーがブラウザで `auth.openai.com/codex/device` を開いてコードを入力し、ログインを済ませる
3. SAIVerse は完了までポーリングし、返ってきた authorization_code を `auth.openai.com/oauth/token` で鍵一式（access_token + refresh_token）に交換する
4. 鍵は SAIVerse 自前のファイルに保存する

`oauth/token` エンドポイントと client_id（Codex CLI のもの）は、既存のトークン再発行 (`llm_clients/openai_codex.py`) が既に使っているものと同一。新規はステップ 1〜2 と保存先だけ。

## 2. 全体の旅路

```
ログイン (新規)                 保存 (新規)                    使用 (既存)              再発行 (既存を一般化)
UI ボタン → デバイスコード → ~/.saiverse/user_data/     → OpenAICodexClient が読む → 401 で refresh_token を
フロー (backend が実行)        codex_auth.json                                        使い、同じファイルへ書き戻す
```

- **ログアウト**: 自前ファイルの削除のみ。`~/.codex/auth.json` には決して触れない。
- **既存ユーザーの移行**: 何もしなくてよい。自前ファイルが無ければ従来どおり `~/.codex/auth.json` に相乗りする。

## 3. 不変条件

1. **refresh_token は一回使うと無効になる。だからトークンの「住むファイル」が再発行の書き戻し先も所有する。** 自前ストアの鍵は自前ストアへ、`~/.codex` から読んだ鍵は `~/.codex` へ（後者は既存実装の維持。CLI と SAIVerse が同じ refresh_token を取り合う事故への対策＝ファイルロック + 拾い直しも既存のまま）。ファイルをまたいだ書き戻しは、片方のファイルに死んだ refresh_token を残し、そちらを使う側のログインを静かに壊す。
2. **自前ストアが存在するとき、それが唯一の認証源。** 自前ストアが壊れて/期限切れでも `~/.codex` へ黙って乗り換えない（別アカウントかもしれない鍵への無断切替は、どのアカウントで喋ったか分からない発話を生む）。エラーは「設定画面から再ログインしてください」と案内する。
3. **トークンをフロントエンドへ渡さない。** ブラウザに出るのは user_code と誘導 URL だけ。ログの出力にもトークンを含めない。
4. **ログインフロー自体は無課金・ペルソナ無関係。** LLM 呼び出しも永続データ（トークンファイル以外）への書き込みも発生しない。

## 4. 責任分界

| 誰が | 何をする |
|---|---|
| ユーザー | 設定画面でログインボタンを押す。ブラウザでコードを入力して OpenAI にログインする。 |
| SAIVerse backend | デバイスコード申請・ポーリング・鍵交換・保存・再発行・ログアウト。 |
| SAIVerse frontend | user_code の表示と状態のポーリング表示のみ。 |
| Codex CLI | もはや不要。ただしインストール済み環境では従来どおり相乗り先として機能し続ける（自前ストアが無い場合のみ）。 |

## 5. 実装の置き場所

- `llm_clients/openai_codex_auth.py`（新規）: デバイスコードフローの状態機械・自前ストアの読み書き・ストア解決（自前 > `~/.codex`）。**「どのファイルが認証源か」の知識はこのモジュールだけが持つ。**
- `llm_clients/openai_codex.py`（改修）: 固定パス `CODEX_AUTH_FILE` 直参照をストア解決経由に置き換え。再発行の書き戻し先を「読んだファイル」にする。
- `api/routes/codex_auth.py`（新規）: `/api/codex-auth/login/start`・`/login/status`・`/status`・`/logout`。
- `frontend/.../ProviderManagementPanel.tsx`（改修）: `openai_codex` プロバイダ行にログイン状態バッジとログイン/ログアウト操作。

## 6. 引き受けるリスク

- **client_id は Codex CLI のものを名乗り続ける。** OpenAI 非公認の利用形態である点は相乗り時代と同質（Hermes Agent・opencode 等の先行例と同じ立場）。OpenAI 側のポリシー変更で止まりうる。
- **デバイスコードの各エンドポイントは非公開 API。** 予告なく変わりうる。止まった場合の脱出路として `~/.codex` 相乗り（= Codex CLI でログインし直す）が残る。
- **同一 client_id の複数ログイン（SAIVerse と Codex CLI が別々の refresh_token を持つ）が共存できるか**は、Hermes Agent が複数アカウントのプールを実働させていることが傍証（推測の域。まはーの実機ログインで確定する）。
- **lease 返却の不達で、閉じたはずのログイン試行が生き残りうる**（レビュー 3 巡目 R3-③ = start 応答の喪失・5 巡目 R5-③ = cancel の配送失敗。4 巡目 R4-② で影響を再評価のうえ族として受容、まはー裁定待ち）。フロントがサーバーへ lease を返せない経路（start 応答が届く前の接続断、cancel POST の失敗）では、閉じる操作でその試行を止められない。最悪ケースは「15 分の試行残置」に留まらない — その窓内にユーザーがブラウザ側の認証を完了すると、**モーダルを閉じて取り消したつもりのログインが成立し、トークンが永続化する**。回復は UI からのログアウト一発（logout は進行中の試行も全て、別プロセスの分まで無効化する）。発生には「localhost 上の HTTP 不達」と「その後にブラウザ認証を完了」の重なりが必要で、成立しても自分のアカウントに自分が入るだけであることから、これを塞ぐ TTL ハートビート・冪等 owner ID・cancel 再送キューの機構は複雑さが釣り合わないと判断して導入しない。
- 既知 issue [api_state_changing_routes_have_no_origin_check](../issues/api_state_changing_routes_have_no_origin_check.md) は本ルートにも当てはまる（既存ルートと同じ姿勢で追随し、個別対策はしない）。

### クライアントの使い分け（実機で確定、2026-08-16）

**認証ホスト（auth.openai.com）への通信は素の httpx を使い、curl_cffi の TLS 偽装は推論ホスト（chatgpt.com/backend-api/codex）だけに使う。** これは好みでなく必須の分岐で、実機一発目で踏んだ地雷から確定した: 認証ホストは、TLS 指紋がブラウザに見えるクライアント（curl_cffi の Chrome 偽装）に対して、API の JSON ではなく**人間向けの HTML ログインページ（200 text/html）を返す**。そのため device-code のポーリングが毎回 JSON パースで落ちた。同じリクエストを素の httpx で投げると正しい `403 {"code":"deviceauth_authorization_pending"}` が返る（生応答で確認済み）。Codex CLI も Hermes Agent も認証ホストには素のクライアントを使っている。トークン再発行 (`openai_codex.py._request_refresh`) も同じ認証ホストなので httpx。

### 観察メモ（Hermes 読解の副産物、未検証・本件のスコープ外）

Hermes は**推論本体**（chatgpt.com）も TLS 偽装なしの素の httpx で通し、「Cloudflare の関門は `originator` ヘッダーの許可リストであって TLS 指紋ではない」とコメントしている。事実なら、推論ホストの `curl_cffi` 依存も過剰装備の可能性がある——ただし彼らの主張の引き写しで未検証（上の認証ホストの話とは別。あちらは確定・修正済み、こちらは推論ホストの話で未確認）。動いているものを剥がす理由が生まれたとき（依存整理など）に思い出すこと。

## 7. 検証の旅路

- **ユニットテスト**: フローの状態機械（申請→ポーリング→交換を偽 HTTP で）、ストア解決の優先順位、再発行の書き戻し先の所有権、ログアウトが `~/.codex` に触れないこと。
- **実機**: まはー自身のブラウザ操作を伴う本物のログイン 1 回 + そのトークンでの発話 1 回（発話はまはーの通常利用に任せる。ここは私では代行できない領域）。
- **未検証のまま渡す境界**: OpenAI 側エンドポイントの実挙動（レート制限・エラー形）は Hermes の実装から写した推定を含む。実ログインで初めて確定する。
