# SAIVerse Remediation Runbook

- 作成日: 2026-03-20
- 対象: `C:\Users\ryo-n\Codex_dev\SAIVerse`
- 参照レビュー: `docs/technical_debt_review_20260320.md`

## 目的

この Runbook は、2026-03-20 時点で洗い出した技術的負債とコードレビュー指摘に対して、

- 何を直すか
- どの順番で進めるか
- 何をもって完了とするか
- どのテストを必ず通すか

を実作業向けに整理したものです。

## 基本方針

1. まずはセキュリティと運用事故のリスクを下げる。
2. 次に、影響範囲が比較的小さい入口側のガードを入れる。
3. その後で、実行系の共通化やフロントの分割に進む。
4. 各修正は「コード変更」と「テスト追加または更新」をセットで行う。
5. 修正後に手動確認だけで終わらせず、backend / frontend の検証手順を残す。

## 対象ファイルと優先順位

### フェーズ 1: 先に守りを固める

- `api/routes/system.py`
- `api/routes/world.py`
- `main.py`

### フェーズ 2: 実行経路の安全化

- `manager/blueprints.py`
- `manager/runtime.py`
- `saiverse/saiverse_manager.py`

### フェーズ 3: フロントの安定化と整理

- `frontend/src/app/page.tsx`
- 必要に応じて新規 hook / service / component

### フェーズ 4: 品質基盤の整備

- `requirements.txt`
- `pyproject.toml`
- `ruff.toml`
- `frontend/package.json`
- 必要に応じて `requirements-dev.txt` または同等の開発依存定義
- CI 設定ファイル

## フェーズ 1

### 目的

- ブラウザ経由での破壊的操作リスクを下げる
- 起動時に他プロセスを巻き込む事故を防ぐ

### 実施内容

- `api/routes/system.py`
  - `/update` にローカル限定ガード、確認トークン、または認証を追加する
  - 破壊的 API を無条件で叩けないようにする
- `api/routes/world.py`
  - world 系の変更 API に認可または最低限の保護を入れる
  - ツール登録 API に入力制約を加える
- `main.py`
  - CORS を最小権限に見直す
  - `allow_credentials=True` とワイルドカード併用を解消する
  - ポート掃除時に無関係な PID を kill しない設計へ変更する

### 完了条件

- 外部ページから無認証で更新・設定変更 API を叩けない
- SAIVerse 起動時に別アプリの PID を自動 kill しない
- CORS 設定が開発用と本番想定で整理されている

### チェックリスト

- [ ] `api/routes/system.py` に保護ロジックを追加した
- [ ] `api/routes/world.py` に保護または入力制約を追加した
- [ ] `main.py` の CORS 設定を見直した
- [ ] `main.py` のポート掃除ロジックを安全側へ変更した
- [ ] 変更内容をドキュメント化した

## フェーズ 2

### 目的

- 危険なツール登録・実行を抑制する
- 実行ロジックの重複を減らす

### 実施内容

- `manager/blueprints.py`
  - `module_path` と `function_name` の登録時バリデーションを追加する
  - allowlist または builtin 制限を導入する
- `manager/runtime.py`
  - ツール実行前の検証を追加する
  - 実行失敗時のエラー処理を明確化する
- `saiverse/saiverse_manager.py`
  - 重複している `execute_tool()` を共通化する
  - 実行経路を 1 つに寄せる

### 完了条件

- 任意モジュール・任意 callable を登録できない
- ツール実行前に検証が必ず通る
- ツール実行ロジックが 1 箇所にまとまっている

### チェックリスト

- [ ] `manager/blueprints.py` に登録制約を追加した
- [ ] `manager/runtime.py` に安全確認を追加した
- [ ] `saiverse/saiverse_manager.py` の重複ロジックを整理した
- [ ] 既存 builtin tool が壊れていないことを確認した
- [ ] エラーメッセージが利用者視点で分かる形になっている

## フェーズ 3

### 目的

- フロントの履歴同期を壊れにくくする
- 巨大コンポーネントの責務を分離する

### 実施内容

- `frontend/src/app/page.tsx`
  - メッセージ対応付けを `role + content prefix` 依存から脱却する
  - stable temporary ID または request correlation を導入する
  - ポーリング、履歴同期、ストリーミング、再接続を分離する
  - 不要な debug log を整理する
- 必要に応じて
  - `hooks/useChatHistory.ts`
  - `hooks/useChatStreaming.ts`
  - `hooks/useReconnect.ts`
  - `services/chatSync.ts`
  などへ分離する

### 完了条件

- 同じ書き出しのメッセージが続いても誤同期しない
- `page.tsx` の責務が分離されている
- デバッグログが常時大量出力されない

### チェックリスト

- [ ] フロントのメッセージ同期方式を変更した
- [ ] `page.tsx` から同期・通信ロジックを切り出した
- [ ] ポーリングとストリーミングの競合を確認した
- [ ] デバッグログを整理した
- [ ] UI の既存挙動が保たれていることを確認した

## フェーズ 4

### 目的

- 修正を継続的に守るための品質基盤を作る
- 「直したのに再発する」を減らす

### 実施内容

- Python 側
  - `pytest` を明示的に導入する
  - `ruff` の設定を 1 箇所へ統一する
  - 開発依存を明文化する
- Frontend 側
  - test script を追加する
  - 必要なら `vitest` または `jest` を導入する
- CI
  - backend test
  - backend lint
  - frontend lint
  - frontend test
  を自動実行する

### 完了条件

- 新しい開発環境でテスト・lint を再現できる
- CI が最低限の品質ゲートとして機能する

### チェックリスト

- [x] Python の開発依存を整理した
- [x] pytest を実行できるようにした
- [x] ruff 設定を統一した
- [x] frontend の test script を追加した
- [x] CI で backend / frontend の検証が回るようにした
- [x] 実行手順を docs に反映した

## テスト方針

今回の改善では、コード修正と同時に次のテストを回す前提とします。

### backend

- 単体テスト
  - 変更した関数・ルートごとのテストを追加する
- 回帰テスト
  - API の保護ロジック
  - ツール登録バリデーション
  - ツール実行の拒否ケース
  - 起動処理の安全側挙動

### frontend

- 単体テスト
  - メッセージ同期
  - ポーリング
  - ストリーミング後の確定処理
- lint
  - 変更ファイルの静的検証

### 手動確認

- 更新 API が無条件では実行できない
- world 系 API が保護されている
- SAIVerse 起動で他プロセスを落とさない
- チャット送信、ストリーミング、履歴読み込み、再接続が壊れていない

## 追加すべきテスト

### `api/routes/system.py`

- 保護なしリクエストが拒否されるテスト
- 正常な条件でのみ update が走るテスト

### `api/routes/world.py`

- 不正な tool 登録が拒否されるテスト
- 許可された tool のみ登録できるテスト

### `manager/blueprints.py`

- allowlist 外の `module_path` を拒否するテスト
- 危険な関数名を拒否するテスト

### `manager/runtime.py`

- 無効な tool が実行されないテスト
- 実行前検証の失敗時に分かりやすいエラーを返すテスト

### `frontend/src/app/page.tsx`

- 同一 prefix のメッセージでも誤同期しないテスト
- ストリーミング完了後に正しい ID が付与されるテスト

## 実行コマンド案

実際のコマンドは環境整備後に統一するが、最低限この形で回せる状態を目指す。

```bash
# backend
python -m pytest
python -m ruff check .

# frontend
npm run lint
npm run test
```

## 作業開始前チェックリスト

- [ ] 作業対象フェーズを決めた
- [ ] 影響対象ファイルを確認した
- [ ] 既存テストの有無を確認した
- [ ] 追加するテスト内容を決めた

## PR 前チェックリスト

- [ ] コード変更に対応するテストを追加または更新した
- [ ] backend test を実行した
- [ ] backend lint を実行した
- [ ] frontend lint を実行した
- [ ] frontend test を実行した
- [ ] docs を更新した
- [ ] 重大リスクの再発防止観点が含まれている
