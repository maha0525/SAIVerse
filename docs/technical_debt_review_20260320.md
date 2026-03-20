# SAIVerse 技術的負債・コードレビューまとめ

- 作成日: 2026-03-20
- 対象: `C:\Users\ryo-n\Codex_dev\SAIVerse`
- 観点: 実害のある不具合リスク、保守性、運用安全性、検証体制

## 要約

今回のレビューでは、特に以下の 3 点が重大でした。

1. ブラウザから到達できる破壊的 API が広く公開されている。
2. 起動時のポート掃除が無関係なプロセスまで終了させ得る。
3. ツール実行機構が任意モジュール import に近く、安全境界が弱い。

加えて、フロントエンドの単一巨大コンポーネント化、重複ロジック、テスト・lint の運用不備が、今後の修正コストと回帰リスクを押し上げています。

## 重大な指摘

### 1. `localhost` 前提の破壊的 API が広く公開されている

- `FastAPI` 側で `allow_origins=["*"]`、`allow_methods=["*"]`、`allow_headers=["*"]` を許可している。
- その状態で `/api/system/update` が認証なしで updater 起動とプロセス終了を行える。
- `/api/world/*` も同様に保護がなく、設定変更・削除系 API がブラウザから直接叩ける。
- README では Tailscale 公開も案内されており、「完全ローカルだから安全」という前提が崩れやすい。

影響:

- 悪意あるページやローカルネットワーク経由のアクセスで、更新・停止・設定改変を誘発される可能性がある。

主な参照:

- `main.py:389-395`
- `api/routes/system.py:151-240`
- `api/routes/world.py:218-228`

### 2. 起動時のポート掃除が無関係なプロセスを kill し得る

- 起動前に、指定ポートを使っている PID を見つけて強制終了する実装になっている。
- 対象プロセスが SAIVerse 自身かどうかの確認がない。
- 3000 番や API ポートを別用途で使っていると、他の開発サーバーやアプリを巻き込む。

影響:

- ユーザー環境の破壊、他プロジェクトへの干渉、原因の分かりにくい障害。

主な参照:

- `main.py:75-124`
- `main.py:127-163`
- `main.py:165-194`

### 3. ツール実行機構の安全境界が弱い

- ワールド API から `module_path` と `function_name` をそのまま登録できる。
- 実行時は `importlib.import_module()` と `getattr()` でロードしている。
- allowlist や署名済み builtin 限定の仕組みが見当たらない。

影響:

- 誤設定でも危険な callable を実行経路へ乗せられる。
- 将来的に UI や API の露出が増えた際、権限昇格や任意コード実行に近い問題へ発展しやすい。

主な参照:

- `api/routes/world.py:218-224`
- `manager/blueprints.py:343-392`
- `manager/runtime.py:617-690`

## 技術的負債

### 4. フロントエンドのメッセージ同期ロジックが壊れやすい

- 楽観更新後の同期で、メッセージの対応付けを `role + content の先頭120文字` で推定している。
- 定型文や似た書き出しが続くと、別メッセージへ ID・usage・avatar を誤反映する可能性がある。
- その結果、重複除去、ポーリング、スクロール復元まで連鎖的に壊れ得る。

主な参照:

- `frontend/src/app/page.tsx:492-545`
- `frontend/src/app/page.tsx:1388-1407`

改善方向:

- クライアント側で stable な temporary ID を発行し、サーバーがそれを引き継いで返す。
- 内容一致ではなく request/response correlation で同期する。

### 5. `page.tsx` への責務集中

- `frontend/src/app/page.tsx` が 2,000 行超で、履歴取得、ストリーミング、ポーリング、再接続、添付、チュートリアル、通知まで抱えている。
- 状態遷移の境界が曖昧で、局所修正が別機能へ影響しやすい。
- デバッグログも大量に残っており、追跡しやすい反面、恒常運用コードとしてはノイズが多い。

主な参照:

- `frontend/src/app/page.tsx:440-571`
- `frontend/src/app/page.tsx:790-817`
- `frontend/src/app/page.tsx:1221`

改善方向:

- 履歴同期、ストリーム処理、接続監視、添付管理を hook / service / reducer に分離する。

### 6. ツール実行ロジックの重複

- `manager/runtime.py` と `saiverse/saiverse_manager.py` にほぼ同じ `execute_tool()` 実装が存在する。
- 安全対策や例外処理を片方だけ直す drift が起きやすい。

主な参照:

- `manager/runtime.py:617-690`
- `saiverse/saiverse_manager.py:908-983`

改善方向:

- ツール解決と実行を単一サービスへ寄せる。
- API/UI/自律実行の全経路で同じガードを使う。

### 7. デバッグ出力が本番コードに強く残っている

- `people/recall.py` で `print()` ベースの詳細ログが複数残っている。
- フロントでも `console.log("[DEBUG] ...")` が多く、運用時のノイズや性能低下の原因になる。

主な参照:

- `api/routes/people/recall.py:104-111`
- `api/routes/people/recall.py:176-218`
- `frontend/src/app/page.tsx:440`
- `frontend/src/app/page.tsx:804`

改善方向:

- 構造化 logging に寄せ、環境変数で詳細ログを切り替える。

## 検証体制の負債

### 8. テスト・lint の入口はあるが、実行保証が弱い

- `pyproject.toml` に pytest 設定がある一方、依存には `pytest` が見当たらない。
- `requirements.txt` には `ruff` があるが、実環境の `.venv` には入っていなかった。
- フロントエンドは `lint` script はあるが、test script がない。
- `pyproject.toml` と `ruff.toml` でルール設定が食い違っている。

主な参照:

- `pyproject.toml:37-47`
- `requirements.txt:1-36`
- `ruff.toml:1-40`
- `frontend/package.json:6-10`

確認できたこと:

- `.venv\Scripts\python.exe -m pytest -q` は `No module named pytest`
- `.venv\Scripts\python.exe -m ruff check .` は `No module named ruff`

改善方向:

- 開発依存を `requirements-dev.txt` などで明示化する。
- CI で backend test / backend lint / frontend lint を強制する。
- Ruff 設定を 1 箇所へ統一する。

## 優先順位付きの対応案

### 最優先

1. 破壊的 API に認証またはローカル限定ガードを入れる。
2. CORS を最小権限化し、`allow_credentials=True` とワイルドカード併用をやめる。
3. ポート使用中プロセスの自動 kill をやめ、所有確認またはユーザー確認に切り替える。
4. ツール登録・実行を allowlist 制へ寄せる。

### 次点

1. `page.tsx` の同期・ポーリング・ストリーム処理を分離する。
2. `execute_tool()` を共通サービスへ統合する。
3. `print` / `console.log` ベースのデバッグ出力を整理する。

### 継続的改善

1. 開発依存と実行手順を整理する。
2. CI で最低限の lint / test を必須化する。
3. 大型ファイルを責務分割して保守コストを下げる。

## 備考

- 今回は静的レビュー中心であり、テストは環境依存パッケージ不足のため未実施。
- 指摘は「今すぐ壊れるもの」と「修正コストを増やす負債」を分けて整理した。
- 対応計画と実施チェックリストは `docs/RUNBOOK.md` を参照。
