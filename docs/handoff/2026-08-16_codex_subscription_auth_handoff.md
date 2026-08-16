# Codex サブスク認証の自立 — 実装 + Codex レビュー 8 巡 (approve 到達) の記録

**日付**: 2026-08-16
**書き手**: メティス (Fable セッション)。
**経緯**: 当初は運用規則どおり「レビュー 1 巡 → Opus へハンドオフ」の予定だったが、まはー裁定 (クォータ余剰) で同セッションが消し込みを続投。**計 8 巡で approve (出荷可) に到達**。§8 に全巡の経緯。
**intent**: [codex_subscription_auth.md](../intent/codex_subscription_auth.md) — 不変条件・責任分界・受容済みリスクは全部ここにある。
**残り**: まはーの裁定 1 点 (lease 返却不達の族の受容、intent §6) + 実機ログイン検証 (§4)。コードとしての消し込みは完了。

## 1. 何を作ったか

SAIVerse 自身が ChatGPT アカウントへデバイスコード方式でログインし、Codex サブスクのトークンを自前ストア (`~/.saiverse/user_data/codex_auth.json`) に持てるようにした。Codex CLI への相乗り (`~/.codex/auth.json`) はフォールバックとして無傷で残る。

| 層 | ファイル | 内容 |
|---|---|---|
| 認証コア (新規) | `llm_clients/openai_codex_auth.py` | ストア解決 (自前優先→CLI) / デバイスコードフロー状態機械 / JWT claim 読み / 共有定数の正典 |
| クライアント (改修) | `llm_clients/openai_codex.py` | 固定パス直参照→ストア解決経由。refresh の書き戻し先 = 読んだファイル (所有権) |
| API (新規) | `api/routes/codex_auth.py` + `api/main.py` | `/api/codex-auth/` 5 本 (login/start・login/status・login/cancel・status・logout) |
| UI (新規+改修) | `CodexLoginModal.tsx/.module.css`, `ProviderManagementPanel.tsx` | openai_codex 行に状態バッジ + ログイン/ログアウト。ダークモード対応済み |
| テスト (新規) | `tests/llm_clients/test_openai_codex_auth.py` | 23 本 (フロー / ストア優先 / 書き戻し所有権 / logout 安全性 / API ルート) |

実装コミット時点: 新規テスト 23 + 既存 llm_clients 75 + フルスイート 4435 全緑、ruff clean、frontend tsc clean。

## 2. Codex 1 巡目の判定 — No-ship (high 3 + medium 1)

観点は「refresh_token 単回使用の所有権 / ストア解決不変条件 / 状態機械の競合 / トークン漏洩 / logout 事故 / フロント後始末 / 相乗り回帰」。**4 件とも私 (メティス) がコードに照らして裏取り済み — 全件実在し、全件受諾**。誇張・空振りは無かった。

### F1 (high) cancel 済み・旧世代のログインスレッドが新試行を上書きする

`openai_codex_auth.py` の `CodexDeviceLoginManager`。cancel は共有 boolean を立てるだけで、①ポーリングが認可コードを取った後〜`_persist_login` 完了までの区間で再確認しない (cancel してもトークンが保存され success に戻る)、②cancel 後の再 `start()` が boolean を False に戻すため、join されずに生きている旧スレッドが復活し、新試行の状態やストアを上書きできる。

**直し方 (Codex 提案・私も同意)**: 試行ごとに世代 ID (または試行ごとの `threading.Event`) を持たせ、ネットワーク往復のたび・交換前・保存前・状態更新前に「自分が現行世代か」を確認。旧世代からの保存と状態更新を禁止。再 start は旧スレッドの終了確認 (join) か世代切替のどちらかで安全化。競合の決定的テストを追加。

### F2 (high) login 保存・refresh・logout が同じロックで直列化されていない

refresh (`openai_codex.py._refresh_or_pickup_latest`) だけが `.json.lock` を取り、`write_auth_store` (ログイン保存) と `delete_saiverse_store` (logout) は素通り。さらに logout はロックファイル自体も消す。実害シナリオ: refresh 中に logout → refresh がファイルを再生成してログアウトが巻き戻る / ログイン保存と refresh が固定名 `.json.tmp` を取り合う / POSIX では保持中ロックファイルの unlink で相互排他自体が破れる。

**直し方**: SAIVerse ストアへの全書き込み・削除 (`_persist_login` / `write_auth_store` 呼び出し元 / `delete_saiverse_store`) を同一 FileLock で直列化。logout でロックファイルを消さない。一時ファイル名は書き込みごとに一意 (pid+乱数等)。logout↔refresh・login↔refresh の順序を再現するテストを追加。

### F3 (high) トークンストアに秘密ファイル権限を設定していない

`write_auth_store` は既定 umask のまま (POSIX で 0644/0755)。他ローカルユーザーから読める。

**直し方**: 親ディレクトリ 0700・ファイルと一時ファイル 0600 (`os.chmod` / `os.open` with mode)。置換後の権限検証。POSIX 権限テスト (Windows では skip マーカー)。

### F4 (medium) start 応答前にモーダルを閉じると cancel が飛ばない

`CodexLoginModal.tsx`: `handleClose` は `state === 'waiting'` のときだけ cancel を送るが、`login/start` の応答待ち中 (state 'idle') に閉じると裏の 15 分ポーリングが残る。ユーザーがブラウザ側を完了すると閉じた後にトークンが保存されうる。

**直し方**: 開始要求を出した時点から「試行中」として扱い、close/unmount では状態に関わらず cancel を送る。F1 の世代 ID を API に載せ (start が試行 ID を返し cancel に渡す)、遅延した旧 start/cancel が新試行を壊さない形に。

## 3. 消し込みの結果 (同セッションで完了)

F1〜F4 は下記 §8 の消し込みループで全て解消 (F1+F4 は世代 ID → lease 方式へ発展、F2 は単一ストアロック + 一意 tmp、F3 は 0600)。最終形の要点:

- **試行の世代 + lease (貸出札)**: start ごとに一意 lease を発行、cancel は lease の返却、全返却で試行停止。保存・状態更新は「現行世代かつ未 cancel」の関所を manager ロック内で通る。
- **logout marker (永続 nonce)**: logout のたびにストアロック下で新しい乱数へ交換。試行は開始時に snapshot (初回はロック下で初期化 — "" は正当値として存在しない)、保存直前にストアロック下で等値再照合。**別プロセスの logout も、破損・削除の ABA も、すべて保存拒否側に倒れる (fail-closed)**。
- **フロント UI 世代**: 開閉・リトライのたびに番号を進め、遅延した旧応答 (start も poll も) は画面に触らず自分の lease を自力返却。unmount でも lease 返却。

## 4. 消し込み後のまはー実機検証 (私では代行できない領域)

1. SAIVerse 起動 → グローバル設定 > モデル管理 > プロバイダ → `openai_codex` 行に「未ログイン」バッジと「ChatGPT でログイン」ボタンが出ること (相乗り環境では「Codex CLI の認証を利用中」)。
2. ログイン → コード表示 → ブラウザで `auth.openai.com/codex/device` にコード入力 → モーダルが「ログインしました」になり、`~/.saiverse/user_data/codex_auth.json` が生成されること。
3. Codex 経由モデルで発話 1 回 (通常利用でよい) — 自前ストアのトークンで通ること。
4. ログアウト → バッジが戻り、`~/.codex/auth.json` が無傷なこと。
5. intent §6 の未確定点の確定: SAIVerse ログイン後も Codex CLI 側のログインが生きているか (`codex` を一度起動して確認)。

## 5. 経緯メモ

- 発端はまはーの「Hermes Agent が直接 OpenAI の認証ページを呼んでいた」という観察 (2026-08-16)。Hermes (NousResearch) の実装読解でデバイスコード方式と判明し、同日着手。読解の副産物 (curl_cffi 過剰装備の可能性・複数ログイン共存の傍証) は intent §6 に記録済み。
- Hermes のクローンは `C:\Users\shuhe\AppData\Local\Temp\hermes-agent` に残してある (参照用・一時領域なので消えても再クローン可)。

## 8. 消し込みループの経緯 (2〜8 巡目、同日夜)

各巡の指摘は全件コード照合で裏取りしてから対応を決めた。誇張・空振りは 8 巡を通じてゼロ。

| 巡 | 判定 | 指摘と対応 |
|---|---|---|
| 2 | No-ship | F1 の cancel が世代を共有し破れる / logout が試行を止めない / attempt_id 省略経路 / STARTING 並行 start → **lease 方式 + logout 統合 + 引数必須化 + start 直列化** |
| 3 | No-ship | logout 後に queued start が復活 / unmount で lease 未返却 / start 応答喪失の孤児 lease → **logout epoch + unmount cleanup。孤児 lease は受容 (intent §6)** |
| 4 | No-ship | 旧 cleanup が新 lease を巻き込む / 受容の影響過小評価 → **フロント UI 世代方式 (待ち合わせ廃止) + intent の最悪ケース明記** |
| 5 | No-ship | logout が別プロセスに届かない / 旧 poll 応答の上書き / cancel 配送失敗 → **logout marker の永続化 + poll 世代ガード。配送失敗は R3-③ と同族として受容へ** |
| 6 | No-ship | epoch counter の破損 ABA / retry が世代を進めない → **counter → 乱数 nonce (fail-closed) + start ごと世代 bump** |
| 7 | No-ship | 欠落値 "" 自体が ABA の再利用点 → **初回 snapshot 前のロック下初期化 ("" の正当値を消滅)** |
| 8 | **approve** | 出荷を阻む残存なし。受容済みの lease 返却不達族は判定から除外の明示 |

テストは 23 → 44 本 (POSIX skip 1 込み)。フルスイートは各巡で回し続け最終 4455 緑。教訓は memory へ: 安全装置の値空間に「再利用可能な点」(counter の 0、欠落の "") を残すと ABA で破れる — nonce + 初期化で点を消す。
