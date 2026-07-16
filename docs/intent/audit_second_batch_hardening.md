# Intent: 監査第二陣 — 共通境界の hardening

**ステータス**: 実装・自動回帰完了、実機確認待ち（2026-07-16）

## 目的

2026-07-12〜15の一次監査で見つかった個別findingを、入口ごとの継ぎ当てではなく共通境界へ集約して閉じる。Discordは導線ごとの再設計候補であるため対象外とする。

## まはー承認済みの判断

1. LAN利用は残す。既定はloopback、明示LAN modeでは単一owner認証を必須にする。
2. 任意absolute pathは禁止し、SAIVerse管理rootとユーザーが明示登録した外部mountだけを許可する。
3. 暗黙paid fallbackは禁止する。将来はmodel単位の任意fallback chainを実装するが、今回はprocess-global paid固定を止める安全化を先行する。詳細は `model_provider_management.md` の将来Phaseを正典とする。
4. 未署名third-party Addonは明示警告・確認付きで許可を残す。公式catalogはHTTPS、full object ID、署名を必須とする。

## 不変条件

### 実行能力の入口は一つ

text spell、pre-spell、realtime spell、Playbook node/finalizer、native/MCP tool、Addon actionは同じ中央認可を通す。実効権限は呼出元権限と呼出先要求権限の積集合で、Playbookや一部wrapperを経由して昇格しない。

### user utteranceのdurabilityはPulseより先

ユーザー原文をdurable storageへ確定しなければ、LLM・tool・課金・外部副作用を開始しない。`client_message_id`はcommand全体のidempotency keyとし、同一keyの再送は副作用を再実行しない。

### 外部mutatorを推測で再実行しない

自動retryは出力前のread-only/idempotent操作だけに限定する。streaming開始後、投稿・device操作・file生成など適用成否不明の状態では再実行しない。

### secretはwrite-only

password、OAuth access/refresh token、provider credentialのAPI応答はmaskと`is_set`に限定する。credentialを任意endpointと組み合わせず、provider recordと接続先policyへ束縛する。

### world backup / restore / updateの正典は一つ

world snapshot、restore、update前復元点は共通engineへ集約する。restoreは停止状態だけで実行し、全archive/DB検証、staging、swap、rollbackを一単位にする。

### ペルソナ `memory.db` 個別backupは統合対象外

`sai_memory.backup`による各ペルソナのrdiff/simple backupは、world snapshotとは目的・retention・復元粒度が異なる独立安全網として維持する。

- 稼働DB: `~/.saiverse/personas/<persona_id>/memory.db`
- 個別backup: `~/.saiverse/backups/saimemory_rdiff/<persona_id>` または `saimemory_simple/<persona_id>`
- world snapshotは稼働中の `memory.db` をworld世代の一部として含むが、既存 `backups/` tree自体は再帰包含しない。
- world経路の一本化は、個別backupの起動時実行、rdiff履歴、simple世代、persona単位restore導線を削除・上書きしない。

### version不整合・chain欠落はfail closed

upgrade registry import失敗、handler遷移graphの穴、未来version、変換不能dataは起動失敗とする。City移動はprotocol互換rangeが設計されるまで完全一致versionだけを受理する。cross-DB副作用は実行台帳で冪等化する。

## 実装結果

- Tool / Playbook / Addon actionは共通の実行時authorization wrapperを通り、実行直前にもAddon有効状態・Aspect spell権限・allowlistを再検査する。
- user utteranceはPulse/LLM/toolより先に永続化し、同じ `client_message_id` の再送は同じuser rowへ収束する。
- streaming出力開始後および結果不明の外部mutatorは自動retryしない。routerの例外文字列ベースfree→paid切替とprocess-global paid固定は撤去した。
- LAN公開時はowner token、許可Origin、HttpOnly session cookieを必須化した。secret API応答はmask＋`is_set`、file pathはmanaged root、uploadはhard limitを共通境界にした。
- world snapshot format v2は全memberのSHA-256/size manifest、CRC、必須main DB、全SQLite `integrity_check`を事前検査し、staging treeをrollback可能にswapする。`backups/`は保存・復元対象外なので、persona別rdiff/simple世代は維持される。
- main DBにはmutation前 `pre_upgrade` 世代、独立retention、検証済み世代だけの列挙、停止状態専用restore CLIを追加した。
- updaterは全入口を `scripts/update_engine.py` へ集約し、clean Git fast-forward、更新前world snapshot、phase fail-stop、同一argv/City/DB identityでの再起動、health確認、失敗時code rollbackを共通contractにした。未知processをport番号だけで終了しない。
- upgrade handler registry/chain/versionはfail-closed、external memory通知はidempotent、City移動APIは完全一致versionを要求する。

## 将来のmodel fallback

モデルごとに、順序付きの任意fallback chainを設定できるようにする。free→paidだけでなく、軽量modelや別providerを指定可能にする。typed failure分類、budget/cooldown、循環・存在検証を持ち、出力またはtool副作用開始後はfallbackしない。今回は新規実装せず、`model_provider_management.md`に設計を残す。

## 運用側に残る必須作業

公式Addon catalogの署名検証境界は実装済みだが、公開者の秘密鍵をrepository内で生成・保有してはならない。公式registry運用側でEd25519鍵を生成し、署名済みenvelopeをpublishし、raw public keyを `SAIVERSE_ADDON_REGISTRY_PUBLIC_KEY` として配布するまで、公式catalogは意図的にfail-closedとなる。

旧ZIP overlay updaterは、旧release manifestなしでは「退役tracked file」と「ユーザーが追加した未知file」を安全に区別できないため廃止した。非Git配布を再導入する場合は、署名済みrelease manifestとprotected rootsを持つstaging/mirror swapとして設計する。

## 自動検証

- `pytest tests --ignore=tests/test_avatar_pipeline.py -q`: **2419 passed / 34 subtests passed**。
- `test_avatar_pipeline.py` 118件は、監査対象外の外部 `expansion_data/saiverse-stackchan-addon/avatar_pipeline.py` からテスト前提の `get_addon_storage_path` が消えている既存不整合のため別管理とする。第二陣で変更したコードの失敗ではない。
- Python変更対象の `ruff check`: pass。
- frontend `tsc --noEmit`: pass。ESLintは0 errors（既存warnings 243）。
- `scripts/gen_reference_docs.py --check`: pass。`git diff --check`: pass。
- 全体回帰中に発見したテスト側の `SAIVERSE_HOME` 復元漏れ3箇所と、閉じたevent loopを再利用する順序依存も修正した。
