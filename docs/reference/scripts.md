# スクリプト一覧

`scripts/` にある保守スクリプトの**主要なもの**。全一覧は `ls scripts/`（一回きりの移行・デバッグ用スクリプトも多数あるため、ここは常用のものに絞った curated 版）。

## Playbook

| スクリプト | 用途 |
|---|---|
| `import_playbook.py` | 単一 Playbook JSON を DB にインポート（`--file <path>`） |
| `import_all_playbooks.py` | 全 Playbook を DB にインポート（`--force` / `--dry-run`。安全） |
| `playbook_dry_run.py` | Playbook のドライラン検証 |
| `strip_playbook_line_fields.py` | Playbook の line フィールド除去（移行系） |

## ドキュメント生成

| スクリプト | 用途 |
|---|---|
| `gen_reference_docs.py` | `docs/reference/` の自動生成 doc（tool-catalog / api-endpoints / database-schema）を再生成。`--check` でドリフト検査。ルートの [`gen_reference_docs.bat`](../../gen_reference_docs.bat) から叩く |

## 依存関係

| スクリプト | 用途 |
|---|---|
| `check_lock_platforms.py` | `requirements.lock` の各 pin が Windows / Linux / macOS arm64 / macOS x86_64 × Python 3.11〜3.13 で入るか (wheel か sdist があるか) を PyPI に問う。uv は wheel の有無を見ずに解決するので、lock を作り直したら一度回す。入らない組合せがあれば exit 1。背景は [`docs/intent/dependency_management.md`](../intent/dependency_management.md) §3-2 |

## SAIMemory / 記憶

```bash
# rdiff-backup で差分バックアップ
python scripts/backup_saimemory.py air eris --output-dir ~/.saiverse/backups
python scripts/backup_saimemory.py air --full --verbose

# 期間指定で JSON エクスポート
python scripts/export_saimemory_to_json.py air --start 2025-01-01 --end 2025-12-31 --output air.json

# JSON ログを SAIMemory にインポート
python scripts/import_persona_logs_to_saimemory.py --persona air --reset

# 古いエントリを整理 / 埋め込み再生成 / タグ付与
python scripts/prune_sai_memory.py air --days 365
python scripts/reembed_memory.py air   # 次元が合わないベクトルを 4 系統 (メッセージ / Chronicle / ページ / Fragment) とも作り直す。--force で全件
python scripts/tag_conversation_messages.py air --auto
```

その他: `export_saimemory_native.py` / `import_saimemory_native.py`（ネイティブ形式）、`embed_recall_sources.py`、`extract_memory_notes.py` / `organize_memory_notes.py`（メモ）、`debug_memory.py`。

## Memopedia / Chronicle

> ⚠️ Memopedia / Chronicle の生成・整理は、現在はペルソナの自律行動（`autonomy_memory_organization` / `fragment_organize`）や Metabolism の中で**自動的に**行われる。`build_memopedia.py` 等の手動構築スクリプトは**旧フロー**で、通常は使わない（`export_memopedia.py` などの export 系のみ補助的に残る）。
>
> 手動保守 CLI `maintain_memopedia.py` は **2026-08-05 に削除**した。分割・統合は編纂（P4-a）が後継で、旧 CLI 側は LLM に本文を生成させており本文保存則に違反していた（landscape §9）。
>
> `build_arasuji.py` は例外的に現役: インポート直後など、Chronicle をまとめて前倒し生成したい場合の任意ツールとして使う。`--estimate` で LLM を呼ばずに未処理メッセージ数・コール数・概算費用（pricing 設定済みモデルのみ）を表示でき、通常実行時も生成前に同じ見積もりを表示したうえで確認を求める（`--yes` でスキップ可）。
>
> `persona_chronicle_cleanup.py`（2026-07-29 新設）: あらすじのレベル制への移行修復 — 旧コードが作った歪み世代 Chronicle の点検と削除。既定は dry-run（読み取りのみ）、`--execute` はバックアップ・検算・実行台帳掃除込み。手順と現況は `docs/issues/air_aifi_memory_repair.md`。

## インポート（引っ越し）

> 記憶アーキ v2 Phase 4（2026-07-04）: インポートは**挿入＋ローカル埋め込み（無料・LLM 不使用）のみ**で完了とし、完了直後から自動想起（ゾーン C）が機能する。Chronicle（あらすじ）の一括生成は任意・後回しでよい（費用見積もり付き、`build_arasuji.py --estimate`）。詳細は `docs/user-guide/memory-migration.md`。

| スクリプト | 用途 |
|---|---|
| `import_chatgpt_conversations.py` | ChatGPT 公式エクスポートを取り込み |
| `import_chatlog_json.py` | 汎用チャットログ JSON を取り込み |
| `import_persona_logs_to_saimemory.py` | SAIVerse 内の旧形式ログ（`log.json` 等）を取り込み。完了時に埋め込みバックフィル＋サマリを表示 |

## アドオン

| スクリプト | 用途 |
|---|---|
| `addon_install.py` | アドオンのインストール |
| `rebuild_addon_envs.py` | `--core-venv-recreated` (setup.bat / setup.sh が新しい `.venv` を作った回とその続きにだけ付ける) のときと、前の回の記録に本体の `.venv` への入れ直しの失敗があるときは、まず導入済みのアドオンの setup の step のうち本体の `.venv` に入れるもの (`env` の無い `pip_install`) を、保存された答えのまま requirements.lock を constraints にして走らせ直す。続けて、導入済みのアドオン専用の Python 環境 (`~/.saiverse/addon_install/<id>/envs/<name>/`) のうち、フォルダはあるが本体の Python で使えないもの (記録の Python の major.minor が違う・記録が無い・python が無い) と、前の作り直しが途中で止まった記録 (`addon_install/<id>/.setup_in_progress/env-<name>.json`) が残っているもの (フォルダが無くても) を、保存された答えのまま、その環境を使う setup の step を走らせ直して作り直す (setup.bat / setup.sh が新しい `.venv` にパッケージを入れたあと、`.venv` の python で呼ぶ)。アドオンのバージョン・答え・データは変えない。SAIVerse が動いている (または分からない) ときは作り直さない。失敗しても終了コード 0 で、失敗したアドオンを表示し、リポジトリ直下の `.addon_env_rebuild_failures.json` に書く (失敗が無ければ消す) ([intent §2-7](../intent/dependency_management.md)) |

## データ移行

一回きりのマイグレーション群（実行前に `--dry-run` があるものは必ず確認）:

```bash
python scripts/migrate_to_user_data.py --dry-run   # 既存データを ~/.saiverse/user_data 構造へ
```

その他: `migrate_building_logs_to_db.py` / `migrate_conscious_log_to_db.py` / `migrate_memory_tags.py` / `migrate_playbooks_to_lines.py` / `migrate_tasks_db_to_unified.py` / `migrate_track_tasks_json.py`。

`migrate_building_logs_to_db.py` (旧 log.json → building_messages) と `migrate_conscious_log_to_db.py` (旧 conscious_log.json → persona_pulse_cursor) は 2026-08-16 から実体が `saiverse/legacy_log_import.py` にあり、バージョンアップグレード (`0.3.0.dev5`) で自動実行される。スクリプトは個別復旧・再実行用の入口。スキップ判定は「現物のファイルが読めるか」だけで行い、`log.json.corrupted_*` マーカーの有無では判定しない。取り込み漏れは毎起動の検算が UI バナーに出す (詳細: `docs/intent/building_memory_unified.md` の「過去ログ取り込みの自動化と検算」)。`migrate_building_logs_to_db.py` を部屋の指定 (`--building-id`) なしで実行したときは、DB に登録された部屋の ID からファイルの場所を決める。DB に登録されていない部屋のフォルダにある log.json は取り込まず、buildings の直下のフォルダにあるものはログに WARNING を出す (2026-09-11 — フォルダ名を部屋 ID に使うと、macOS でフォルダ名が濁点の分解された形のとき、DB の部屋と別の部屋 ID で書き込むため)。

## 開発 / 運用

| スクリプト | 用途 |
|---|---|
| `update_engine.py` | 全update入口の正典。clean Git fast-forward、更新前world snapshot、phase fail-stop、同一条件restart、health確認、失敗時rollback。依存更新を始める前にリポジトリ直下の `.update_complete` を消し、全段が成功したときだけ「VERSION + requirements.txt / requirements.lock / frontend/package-lock.json の sha256」を JSON で刻み直す (一時ファイル + `os.replace` で atomic)。途中で死ねば印は必ず無く、次回起動は実在照合を通る。画面の「Update」ボタンからの更新 (と チャンネル切り替え) では、バックエンドの終了後にこのフォルダの画面のサーバー (`frontend` で動く node と、start.bat の「SAIVerse Frontend」の窓) も止め、依存を入れ直したあと本番モード (`next start`) だったなら `npm run build` し、バックエンドの健全性確認のあとに同じモードで見える窓として立て直す。バックエンドも見える窓で再起動する。バックエンド停止後のどの段で失敗しても、(必要なら巻き戻して) 元のバージョンのバックエンドと画面を立ち上げ直してから失敗として終える。`--manual` (update.bat / update.sh / 起動時の仕上げ) は、このフォルダの画面のサーバーが動いていれば何も変えずに断る ([issue](../issues/archive/ui_update_fails_while_frontend_runs.md))。パッケージの入れ直し (`update_dependencies`) は、取ってきたばかりのディスク上の `update_engine.py` を子プロセス (`--install-dependencies`) として起動して行う。子は、Windows で `.node` (portable Node.js) があれば `npm ci` の前に `install_node_portable.ps1` を呼んで版が固定と違えば入れ替え、pip / npm のあとに SearXNG (`scripts/.searxng-venv` があるとき) を `searxng_install.py check` で確かめ、違えば `setup` で入れ直す。SearXNG の setup が途中で止まった記録 (`.setup_in_progress/searxng-venv.json`) があるときは、venv の python が無くても、確かめを飛ばして本体の Python で `setup` を動かす。Node.js と SearXNG の入れ替えに失敗しても更新は止めず、警告して続ける。巻き戻しでは Node.js を戻さない (子に `--keep-portable-node` を渡す)。子は成功すると `.update_dependencies_complete` に完了の印と同じ形の記録を書く。npm は `.node` のものを優先し、そのフォルダを PATH の先頭に置いて動かす ([intent §2-6](../intent/dependency_management.md))。画面の「Update」ボタンからの更新では、画面のサーバーと同じく、このフォルダの SearXNG のサーバー (SearXNG の venv の python で `-m searx.webapp` を動かしているものと、start.bat の「SearXNG」の窓) も止め、終わったら (失敗して元の版に戻したときも) バックエンドを起動する前に start.bat と同じ窓で起動し直す。起動に失敗したら `.searxng_restart_failed.json` に書き、起動し直したバックエンドが起動時に画面のバナーに出して消す ([intent](../intent/update_completes_with_new_code.md)) |
| `update_engine.py --check-complete` | 起動前の「更新が仕上がっているか」検査 (start.bat / start.sh が呼ぶ)。コードは書き換えない。終了コード 0=起動可 / 10=`--manual` で仕上げが必要 / 11=判定できなかったので警告して起動続行 / 12=パッケージの入れ直しを試したが失敗したので警告して起動続行。完了の印が一致しているのに `.update_dependencies_complete` が無い (または一致しない) ときは、古い更新処理で更新されたか、入れ替えが失敗したままと判定し、新しいリリースを取らずに `--install-dependencies` を一度だけ実行する。`frontend/package.json` が宣言する部品 (dependencies。devDependencies は `NODE_ENV=production` の npm ci が入れないので数えない) が `node_modules` に欠けていても、この場合と、記録が一致していても失敗の記録 (`.update_dependencies_failed.json`) があるときは、10 ではなくこの入れ直しに回す (入れ直しの npm ci が途中で失敗した跡なので)。10 を返すのは、印と記録が一致して失敗の記録も無いのに部品が欠けているときだけ。印が一致していれば、SearXNG の setup が途中で止まった記録 (`.setup_in_progress/searxng-venv.json`) があるときも、記録が一致していてもこの入れ直しに回す。入れ直しが失敗したら内容を `.update_dependencies_failed.json` に書き、本体が起動時に画面のバナーに出す (次に成功すると消える)。完了の印が無い新しい導入では、入っている部品を直接確かめて問題が無ければ、記録を書き、それが書けたときだけ完了の印を書く。詳細は [issue](../issues/v0229_update_bat_truncates_after_git_pull.md)、[intent](../intent/update_completes_with_new_code.md) |
| `self_update.py` | 旧セルフアップデート入口から `update_engine.py` への互換wrapper |
| `init_git_repo.py` | ZIP を解凍したフォルダを、自動更新のために Git の管理下へ置く (setup.bat / setup.sh が呼ぶ)。Git の記録は「main の最新」ではなく、フォルダの中身と同じバージョン (`VERSION` と同じ名前のタグ。main の履歴に含まれるものだけ) に合わせ、合うタグが無いときだけ main の最新にする。最後にファイルと記録が一致しているかを確かめ、一致しなければその場で知らせる。フォルダの中のファイルは書き換えない。`git init` の直後で止まった導入は、もう一度実行すれば続きから仕上がる。終了コード 0=一致 / 1=Git の操作に失敗 / 2=ファイルと記録が一致しない ([issue](../issues/setup_from_older_zip_blocks_update.md)) |
| `setup_venv.py check-stopped / prepare / finish / rollback` | setup.bat / setup.sh が `.venv` を作る前と後に、選んだ Python 3.13 (標準ライブラリだけ) で呼ぶ。`check-stopped` は Python を選んだ直後 (最初の環境の変更より前) に呼び、SAIVerse 本体 (runtime marker。`.env` は本体の `load_dotenv` と同じく SAIVerse のフォルダから上へ探し、SAIVERSE_HOME も同じ規則で解く)・このフォルダの画面のサーバー・SearXNG のサーバーが動いている、または本体が動いていないと確かめられないときは何も変えずに終了コード 1 (既存の `.venv` の python で `update_engine.py --check-stopped` を実行し、それが動かないときは runtime marker だけを標準ライブラリで読む)。画面のサーバーと SearXNG のサーバーを確かめられないときは、窓をすべて閉じたら Enter を押すよう頼んで待つ (Ctrl+C・入力の終わり・端末でないときは終了コード 1。何も導入されていない初めての導入では聞かない)。`prepare --expect-series 3.13` は既存の `.venv` が別の系列 (`pyvenv.cfg` の `version` の行、無ければ `version_info` の行) か、系列が分からないか、壊れていれば `.venv.old-<日時>` に改名し、結果を一行 (`keep` / `create` / `renamed <名前>` / `resume [<名前>]` / `locked` / `error`) で出す。改名・作成の前に `.setup_in_progress/venv.json` (やりかけの記録、`scripts/setup_in_progress.py`) に古い方の名前を書き、書けなければ何も変えずに `error`、改名を断られたら何も変えずに `locked`。記録が残っている (前の回の setup が終わる前に止まった) ときは、`.venv` の様子にかかわらず続きを行う (記録の古い方が戻し先。揃った `.venv` は `resume`、作りかけは消して作り直す)。`finish` はアドオンの入れ直しが最後まで走ったあと (`rebuild_addon_envs.py` が 0 で終わったときだけ setup が呼ぶ) に記録を消し、`.venv.old-<日時>` を `.venv.deleting-<日時>` へ改名してから消す (残った `.venv.deleting-*` も消す)。`rollback <名前>` は新しい `.venv` が失敗したときに古いものを戻し、記録を消す。表示用の文 (日本語と英語) は `scripts/setup_messages/` ([intent §2-7](../intent/dependency_management.md)) |
| `set_version.py` | バージョン刻印 |
| `snapshot.py` | world snapshot format v2のsave/list/inspect/restore/delete。restoreは停止状態だけで実行 |
| `run_discord_gateway_tests.py` | Discord Gateway テスト |
| `check_in_flight.py` | in_flight 台帳の関所 — 次アクション欄の字数超過と過去形マーカー(日付・コミットハッシュ)混入を検査。台帳を触ったセッションの終わりに回す。2026-08-04 解体時の未移送3行のみ行指紋一致の間だけ警告扱い(exit 0=警告のみ可 / exit 1=免除外の違反・表構造不正) |
| `install_node_portable.ps1` | Windows の portable Node.js をリポジトリ直下の `.node` に入れる。スクリプトに固定した版 (`$NodeVersion`) と `.node\node.exe --version` が違えば、nodejs.org の ZIP を sha256 で照合し、`.node` の隣で展開してから名前の付け替えで差し替える。失敗したら元の `.node` に触れずに exit 1。setup.bat (PC に Node.js が無いとき、または `.node` があるとき) と、`update_engine.py` の `update_dependencies` (Windows で `.node` があるとき、`npm ci` の前。更新のたびと、`--check-complete` の一度だけのやり直し) が呼ぶ。start.bat / start-dev.bat はこれを呼ばず、入れ替えの途中で止まって `.node.old` だけが残っているときに `.node` へ名前を戻すだけ。`-ArchivePath` で手元の ZIP から入れられる ([intent §2-6](../intent/dependency_management.md)) |
| `install_vc_redist.ps1` | Windows に Microsoft Visual C++ 再頒布可能パッケージ (x64) が無いときに、Microsoft の配布元から取って入れる (setup.bat が呼ぶ)。記憶の検索に使う onnxruntime がこの部品を必要とし、無いと SAIVerse が起動時に落ちる。取ったファイルが Microsoft の署名つきであることを確かめてから実行する。入れられなかったときは手で入れる場所を知らせ、setup は止めない ([issue](../issues/archive/clean_windows_missing_vc_runtime_blocks_startup.md)) |
| `ensure_searxng_version.py` | SearXNG を起動する直前に呼ばれる (run_searxng_server.ps1 / .sh)。`git` が見つからないときだけ、SearXNG のソースに `searx/version_frozen.py` を書く。SearXNG は起動のときに `git` で自分のバージョンを調べ、`git` が見つからないと落ちるため ([issue](../issues/archive/searxng_needs_git_on_path.md)) |
| `searxng_install.py setup / check / source` | Windows / Linux 共通の SearXNG 導入・実 import 検査・ソースパス取得。`searxng_pin.json` のコミットの公式 ZIP と依存定義を使い、tzdata も専用 venv へ導入する。pip install の前に、上流の requirements.txt の certifi / anyio を `REQUIREMENT_FLOORS` の下限に書き換える。別の Python の系列で作られた venv は退避してから作り直し、作り直しのどこかで失敗したら作りかけを消して退避した venv を元の名前に戻す。退避の前に退避先をリポジトリ直下の `.setup_in_progress/searxng-venv.json` に書き、作り直しが成功するか戻せたら消す (残っている間は `check` が通らず、次の `setup` が入れ直しと退避先の戻しを引き継ぐ)。`setup` の失敗はリポジトリ直下の `.searxng_setup_failed.json` に書き、本体が起動時に画面のバナーに出す (次の `setup` の成功で消える。`update_engine.py` から呼ばれたときは書かない)。setup_searxng.ps1 / .sh と起動スクリプトから呼ぶ ([intent](../intent/searxng_installation.md)) |
| `download_searxng_source.py` / `searxng_config.py` / `merge_searxng_settings.py` | 固定ソースの取得、版・パスの共通解決、設定の3層マージ。旧ソースを残して `.searxng-src/<commit>/` へ導入し、既存の設定・エンジン選択を引き継ぐ |
