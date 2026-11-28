# Intent Document: 世界スナップショットとバックアップ管理 UI

**ステータス: 検証待ち (v0.3.36 に載る)** (2026-11-28 まはーの本番環境で CAS 作成 24.8 秒 / 2 回目 11.7 秒・2 世代合計 6.22 GB・バックアップタブ UI・ライトテーマ・世代別解放容量表示を確認済み。実機での復元操作のみ未確認)

## 芯 (専門用語なしの数行)

世界のスナップショット置き場 (`~/.saiverse/snapshots/`) を、「ファイルの実体を中身ごとに 1 回だけ置いておく倉庫 (`.store/`)」と「世代ごとのファイル一覧表 (`manifests/<名前>.json`)」に分ける。2 回目以降の更新では前回から変わったファイルだけを書き、変わらない画像や DB は共有するので、所要時間もディスク消費も劇的に減る。どの過去世代も単独で復元でき、自動削除はせず、進捗が出ている限りタイムアウトで切らない。

あわせて、CLI を叩かずに済むようグローバル設定に **「バックアップ」タブ** を新設し、世界スナップショットの作成・一覧・復元・削除と、ペルソナごとの記憶 (`memory.db`) バックアップの作成・個別復元を画面から完結できるようにする。また、本番 DB と同じフォルダに混ざっていた DB 自動バックアップを本来の `backups/` へ移し、スナップショットへの二重混入を断つ。

---

## 1. なぜ必要か (実測された問題と運用上の課題)

2026-11-28 に実機 (`~/.saiverse/`・[self_update.log](../../self_update.log)) で計測した結果、アップデート前のスナップショット作成 (`scripts/snapshot.py save`) に **毎回 8〜13 分** (更新全体の約 85〜90%) かかり、`snapshots/` 自体も **8 世代で 50.7 GB** まで膨らんでいた。

1. **単一スレッドの `zlib` 圧縮レベル 6 による CPU 律速**: 埋め込みベクトルを含む `memory.db` はレベル 6 で **24.3 MB/s** しか出ず、24 コアのうち 1 コアだけで約 4.4 分を消費していた (`level=1` なら **109.3 MB/s** で 4.5 倍速、圧縮率差は 3.3 ポイントのみ)。
2. **既圧縮メディアの無意味な再圧縮**: `image/` (1.78 GB) と `user_data/addon_data/` の PNG 群 (1.51 GB) は圧縮しても 97〜99% しか縮まないのに、再圧縮に約 80 秒を消費していた。
3. **毎回フルコピー + 2 パス読み込み**: 前回から 1 バイトも変わっていないファイルも毎回丸ごとハッシュ計算 → 再読み込み → 再圧縮しており、時間もディスクも世代数に比例して増え続けていた。
4. **除外フォルダの中まで潜る走査**: `home.rglob("*")` が除外済みの `addon_install/` (114,704 ファイル) や `backups/` (12,752 ファイル) の奥まで全部潜ってから捨てていた (実測 **9.89 秒** → `os.walk` 枝刈りで **0.06 秒**)。
5. **DB 自動バックアップの二重混入**: `database/backup.py` と `database/migrate.py` が `backups/` ではなく `user_data/database/` に `*.bak` を書き出しており、本番 DB 約 110 MB に対して過去の DB バックアップ約 2.56 GB (85 個 + 孤立 `.bak-shm/.bak-wal` 1,278 個) が毎回スナップショットへ吸い込まれていた。
6. **固定 3600 秒タイムアウト + 無言処理** ([snapshot_timeout_is_fixed_while_world_grows.md](../issues/archive/snapshot_timeout_is_fixed_while_world_grows.md)): 世界の成長に固定値がいつか追い越され、処理中も進捗が出ないため固まったのか進んでいるのか分からない。
7. **CLI 必須の運用負荷**: スナップショットの削除・復元も、ペルソナの `memory.db` バックアップからの個別復元も、現状はコマンドラインや手動ファイル差し替えが必須になっている ([early_access_release.md](early_access_release.md) §6-4 の未決事項でもあった)。特に CAS 倉庫化で「フォルダから ZIP を直接消す」運用から離れるなら、画面から安全に管理できる道が不可欠。

---

## 2. 影響範囲と不変条件 (全体視点)

### 誰が何に頼っているか

- **利用者**: アップデートやチャンネル切替のたびに待たされず、CLI を打たずにグローバル設定の「バックアップ」タブからスナップショットの確認・作成・削除・復元と、ペルソナごとの記憶 (`memory.db`) バックアップの確認・作成・個別復元ができること。
- **ペルソナと世界データ**: `save` は世界データを変更しない読み取り専用操作であること。世界復元・ペルソナ記憶復元は、必ず SAIVerse 停止状態で整合性検証 (`SHA-256` / SQLite `PRAGMA integrity_check`) と復元前の自動退避を済ませてから原子入れ替えを行い、失敗時は元データを無傷で戻すこと。
- **過去のスナップショット (format_version 2 `.zip`)**: 既存の `.zip` アーカイブを壊さず、画面・CLI の両方から一覧・復元・削除できること。

### 絶対に崩さない不変条件

1. **生ファイルへのハードリンク禁止**: SQLite の `.db` は稼働中にその場で書き換わるため、`~/.saiverse/` の実体とスナップショットをハードリンクで共有してはならない。`.store/objects/` は完全に独立した **不変 (immutable)** ファイルとして保存する。
2. **各世代の完全独立**: 増分パッチの鎖にはしない。各マニフェスト (`manifests/<名前>.json`) はその時点の全ファイル一覧を持ち、途中の世代を削除しても他の世代の復元能力は一切損なわれない。
3. **自動削除なし (スナップショットは明示削除のみ)**: 問題に気づくのが数世代後になることがあるため、世界スナップショットは自動で間引かない (2026-11-28 まはー合意)。
4. **GC (孤立オブジェクト掃除) は fail-closed**: 掃除の前に `manifests/` 内の全マニフェストを読み取り、1 枚でも壊れて読めないマニフェストがある場合は **一切オブジェクトを消さずに警告して止める**。
5. **全件コンテンツハッシュ検証 (mtime 省略なし)**: 変更判定にタイムスタンプキャッシュを使わず、毎回ディスクから全対象ファイルの SHA-256 を計算する (並列読み込みで実測 10 GB ≒ 数秒)。
6. **停止必須の操作は必ずオフライン経路 1 本で実行する**: 世界スナップショットの作成・復元、およびペルソナ単体 `memory.db` の復元は、稼働中のファイルロック (Windows `WinError 32`) とメモリ内キャッシュのズレを構造的に防ぐため、アップデートと同じ **detached worker (一時停止 → 実行 → 自動再起動)** 経路を使う。
7. **ペルソナ記憶の個別復元時は head キャッシュを整合させる**: 1 人の `memory.db` だけ過去へ戻したとき、`saiverse.db` の `session_head_snapshot` に未来の Memopedia / コア記憶キャッシュが残らないよう、対象ペルソナの `session_head_snapshot` 行を削除して次回ターンで再構築させる。
8. **失敗をログに隠さない**: detached worker 内の保存・復元が失敗した場合は、元の状態のまま SAIVerse を再起動し、失敗内容を記録ファイルへ書いて起動時の画面バナー (`startup_alerts`) で利用者に知らせる。
9. **役割の分離**: 再生成できるキャッシュ・運用ログ・別系統の自動バックアップ (`backups/` および `user_data/database/*.bak*`) は世界スナップショットに含めず、復元時にも home 側の現物を残す。

---

## 3. 設計詳細

### 3.1 スナップショット倉庫の構造 (format_version 3: CAS)

`~/.saiverse/snapshots/` の配置:

```text
~/.saiverse/snapshots/
├── README.txt                                  # 人間向け案内 (画面または snapshot.bat から管理する旨と .store/ 直接操作禁止)
├── .store/                                     # 内部オブジェクト倉庫 (先頭ドットで誤削除を抑止)
│   └── objects/
│       └── <sha256先頭2文字>/
│           ├── <sha256>.z                      # zlib level 1 圧縮オブジェクト (.db / .json 等)
│           └── <sha256>.raw                    # 無圧縮オブジェクト (.png / .jpg / .mp4 等)
├── manifests/
│   ├── auto_before_update_20261128_....json    # 世代台帳 (format_version: 3)
│   └── ...
└── auto_before_update_20261122_....zip         # 既存の format_version 2 ZIP (そのまま共存)
```

- **マニフェスト形式 (`format_version: 3`)**: `format_version`, `name`, `created_at`, `saiverse_version`, `note`, `file_count`, `total_bytes_uncompressed`, `files` (`{path: {sha256, size, encoding}}`)。
- **無圧縮対象 (`encoding: "raw"`)**: 既圧縮拡張子 (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.avif`, `.mp4`, `.mov`, `.webm`, `.mkv`, `.avi`, `.mp3`, `.ogg`, `.opus`, `.m4a`, `.aac`, `.flac`, `.zip`, `.gz`, `.bz2`, `.xz`, `.7z`, `.zst`, `.br`, `.vrm`, `.glb`, `.woff2`) は `.raw`、それ以外は `zlib` `level=1` で `.z`。
- **同一ハッシュ再利用**: 同じ SHA-256 のオブジェクトが `.raw` または `.z` で既に存在すれば (非空ファイルが 0 バイトでないことも確認)、圧縮・書き込みを一切行わず既存オブジェクトを参照する。
- **原子公開と並列処理**:
  - `os.walk` で `EXCLUDED_FROM_SNAPSHOT` ディレクトリを枝刈り走査 (`dirs[:]` 編集)。
  - `ThreadPoolExecutor` (`min(8, os.cpu_count() or 4)` ワーカー) で 1 MB チャンクの SHA-256 計算 → 未保存オブジェクトのみ `.tmp.<pid>.<tid>` へ書き出して `os.replace` (同一ハッシュの同時書き込みはハッシュ単位ロックで直列化)。
  - 全オブジェクト確定後に `manifests/<name>.json.tmp.<pid>` → `os.replace` で公開。
- **進捗出力**: 2 秒ごとおよびフェーズ切替時に標準出力へフラッシュ付きで進捗行を出す。

### 3.2 進捗ベースの生存監視タイムアウト (`scripts/update_engine.py`)

[snapshot_timeout_is_fixed_while_world_grows.md](../issues/snapshot_timeout_is_fixed_while_world_grows.md) の恒久策。

- 固定の上限時間 `SNAPSHOT_TIMEOUT_SECONDS = 3600` を廃止し、**無進捗タイムアウト (`SNAPSHOT_STALL_TIMEOUT_SECONDS = 300` = 5 分)** へ置き換える。
- `update_engine.py` は `subprocess.Popen` で `snapshot.py save` の出力を逐次読み取り、進捗行をログへ流しつつ最終出力時刻を更新する。進捗が出る限り世界のサイズに依らず待ち、5 分間完全に無出力になった場合だけハングと判断して終了させる。
- 中断・失敗時は、その子プロセス (PID) が残した `manifests/<name>.json.tmp*`、`.store/objects/**/.tmp.<pid>.*`、および旧 `<name>.zip.tmp` を掃除する。

### 3.3 DB 自動バックアップの混入排除 (`database/backup.py` + `database/migrate.py` + `scripts/snapshot.py`)

1. **供給源の統一 (`database/backup.py` / `database/migrate.py`)**:
   - `<saiverse_home>/user_data/database/saiverse.db` の自動バックアップ (`startup`, `pre_upgrade`, `pre_restore`, `building_id_repair`, `building_retirement` 等) と `database/migrate.py` のマイグレーション前バックアップの保存先を、他の全バックアップと同じ **`<saiverse_home>/backups/saiverse_db/`** へ統一する (`resolve_backup_dir(db_path)`。テスト等の独立パスは従来通り `db_path.parent`)。
   - **`.bak-shm` / `.bak-wal` リークの根治**: `backup_saiverse_db` 完了時に `PRAGMA journal_mode=DELETE` を設定し、検証も `mode=ro&immutable=1` で開く。`_prune_old_backups` でも残存 `.bak-wal` / `.bak-shm` を併せて消す。
   - **既存ファイルの冪等移行**: 標準 DB で `backup_saiverse_db` が走る際、`user_data/database/` に残っている既存の `saiverse.db*.bak` + `.bak.json` を `backups/saiverse_db/` へ移動し、対応する `.bak` が無い孤立 `.bak-shm` / `.bak-wal` を掃除する。
   - `get_recent_backups` は新置き場 (`backups/saiverse_db/`) と旧置き場 (`user_data/database/`) の両方を参照する。
2. **スナップショット境界の保護 (`scripts/snapshot.py`)**:
   - 未移行環境でも DB バックアップを吸い込まないよう、`user_data/database/` 直下の `saiverse.db*.bak` / `*.bak.json` / `*.bak-wal` / `*.bak-shm` を `save` から除外し、`restore` 時にも home 側の現物を保全する (`user_data/database/` の非除外ファイルは最大 3 個なので入れ替え分割の窓は極小)。
   - 旧 format v2 ZIP に含まれていた `user_data/database/*.bak*` は `_safe_archive_member` で拒否せず展開のみ許容して swap 対象外とする (`llama_cache` と同じ後方互換)。

### 3.4 グローバル設定「バックアップ」タブと管理 API

CLI を使わずに完結する画面を、グローバル設定モーダル (`GlobalSettingsModal.tsx`) の新タブ **`バックアップ`** (`BackupManagementPanel.tsx`) として設ける。タブ内は分かりやすく **2 つのサブタブ (またはセクション)** に分ける。

#### A. 世界スナップショット管理

- **表示**: 倉庫の合計サイズ (`.store/` 実消費量 + 旧 `.zip` 合計)、世代数、孤立オブジェクトがあれば回収可能サイズ。一覧は新しい順で「名前・作成日時・SAIVerse 版・メモ・形式 (`CAS` / `ZIP`)・サイズ」を表示。
- **操作**:
  - **削除 (即実行・停止不要)**: 確認ダイアログ後、対象世代を削除し、直後に fail-closed GC を自動実行して空き容量を回収。
  - **不要データの掃除 / GC (即実行・停止不要)**: エクスプローラー等で `manifests/` を直接消して孤立オブジェクトが残っている場合に回収。
  - **いまスナップショットを作成 (一時停止 → 実行 → 自動再起動)**: 任意メモを入力して実行。detached worker で停止 → `snapshot.py save` → 自動再起動。
  - **この時点へ復元 (一時停止 → 実行 → 自動再起動)**: 確認モーダルに「復元する世代の名前・日時・バージョン」「現在の状態は `auto_before_restore_*` として自動退避されること」「コードのバージョン自体は変わらず、古い版の世界を戻した場合は再起動時に移行処理が走ること」を明示し、確認チェックを経て実行。detached worker で停止 → `snapshot.py restore <name> --yes` → 自動再起動。

#### B. ペルソナの記憶 (`memory.db`) バックアップ管理

- **対象データ**: `~/.saiverse/backups/saimemory_simple/<persona_id>/memory.db_backup_*.bak` (SAIVerse 標準の SQLite バックアップ。`saimemory_rdiff/` のみ存在する環境ではその旨を案内)。
- **表示**: ペルソナを選択 (DB に登録されているペルソナの表示名 + ID、および削除済みペルソナでもバックアップが残っていれば参照可) → そのペルソナの現在の `memory.db` サイズと、バックアップ世代一覧 (作成日時・サイズ・ファイル名)。
- **操作**:
  - **いまバックアップを作成 (即実行・停止不要)**: `sai_memory/backup.py` の `run_simple_backup(..., skip_if_unchanged=False)` を呼ぶ (SQLite Backup API なので稼働中のまま安全に取れる)。
  - **バックアップ世代を削除 (即実行・停止不要)**: 確認ダイアログ後、指定の `.bak` ファイルを削除。
  - **この記憶へ復元 (一時停止 → 実行 → 自動再起動)**:
    - 確認モーダルに「対象ペルソナ名・復元するバックアップの日時」「現在の `memory.db` は復元前に自動バックアップされること」「このペルソナの記憶 (`memory.db`) のみを戻し、建物の会話履歴や他ペルソナの記憶は変わらないこと」を明示し、確認チェックを経て実行。
    - detached worker がバックエンド停止を確認 → `scripts/restore_persona_memory.py` (または `sai_memory/backup.py` の復元関数を CLI 呼び出し):
      1. 復元対象 `.bak` に `PRAGMA integrity_check` を実行 (壊れていれば一切触らず中止)。
      2. 現在の `personas/<id>/memory.db` を安全退避 (`memory.db_backup_pre_restore_<timestamp>.bak` を `saimemory_simple/<id>/` に作成、自動ローテーション対象外にして消えないようにするか、`pre_restore` 種別で保全)。
      3. 一時ファイルへ SQLite Backup API で展開 + `PRAGMA integrity_check` → 既存の `memory.db` / `-wal` / `-shm` をロールバック退避して原子入れ替え (失敗時は元へ巻き戻す)。
      4. `saiverse.db` の `session_head_snapshot` から対象 `PERSONA_ID` の行を削除 (未来キャッシュの残留を防ぐ)。
    - 自動再起動して復帰。

#### C. API とオフライン実行の共通基盤

- **新規ルータ**: `api/routes/backups.py` (FastAPI の `/api/backups/...` にマウント)。
  - `GET /api/backups/snapshots` — 世界スナップショット一覧 + 倉庫統計 (`.store/` 実サイズ、孤立オブジェクト数/サイズ)
  - `POST /api/backups/snapshots` — 手動スナップショット作成 (detached worker 起動、`{"status": "running", "operation": "snapshot_save"}`)
  - `POST /api/backups/snapshots/{name}/restore` — 世界復元 (事前検証 → detached worker 起動)
  - `DELETE /api/backups/snapshots/{name}` — スナップショット削除 + 自動 GC (即実行)
  - `POST /api/backups/snapshots/gc` — 孤立オブジェクト掃除 (即実行)
  - `GET /api/backups/personas` — バックアップ対象ペルソナ一覧 (表示名・ID・現在 `memory.db` サイズ・世代数)
  - `GET /api/backups/personas/{persona_id}` — 指定ペルソナの `memory.db` バックアップ世代一覧
  - `POST /api/backups/personas/{persona_id}` — 指定ペルソナの即時バックアップ作成 (停止不要)
  - `DELETE /api/backups/personas/{persona_id}/{filename}` — 指定バックアップ世代の削除 (停止不要)
  - `POST /api/backups/personas/{persona_id}/restore` — 指定バックアップ世代からの復元 (事前検証 → detached worker 起動)
- **オフライン実行の統一 (`api/routes/system.py` + `scripts/update_engine.py`)**:
  - 既存の `_launch_detached_updater` / `.update_config.json` に `operation` (`"update"` / `"snapshot_save"` / `"snapshot_restore"` / `"persona_memory_restore"`) を追加。
  - `update_engine.py` の `--config` モードが `operation` に応じて分岐し、バックエンド停止待ち → オフライン作業実行 → (失敗時は `.backup_operation_failed.json` へ記録) → バックエンド自動再起動、を共通の安全手順で行う (コードの更新や `npm ci` やフロントエンド停止は不要なのでスキップし、バックエンド停止→作業→再起動だけで高速に終わる)。
  - 失敗時の記録ファイル `.backup_operation_failed.json` は起動時に読み取って `startup_alerts` バナーに出し、読み取り後に削除する (既存の `.searxng_restart_failed.json` と同じ 1 回限りの知らせパターン)。
  - フロントエンドは `announceUpdateStarted` と同じ再起動待ちバナー機構を流用し (文言は「スナップショットを作成中… / 復元中… / 再起動を待っています」に切り替え)、再起動完了を検知したら自動リロードする。
- **CLI も引き続きフル機能で維持**: `snapshot.bat` / `snapshot.sh` (`save`, `list`, `inspect`, `restore`, `delete`, `gc`, `export`) は画面が開けない緊急時の復旧手段として必須なので、そのまま残す。

---

## 4. 変更の置き場所と関連ドキュメント

- `scripts/snapshot.py`: CAS (format v3) 保存・復元・一覧・検査・削除・GC・エクスポート、枝刈り走査、並列処理、進捗出力、DB バックアップ除外、API から呼べる共通関数群。
- `sai_memory/backup.py`: ペルソナ `memory.db` の一覧取得・安全復元 (`restore_simple_backup`)・`pre_restore` 退避と `session_head_snapshot` クリア。
- `database/backup.py` / `database/migrate.py`: 標準 DB のバックアップ保存先を `backups/saiverse_db/` へ統一、sidecar リーク修正、既存ファイルの冪等移行。
- `scripts/update_engine.py`: 進捗逐次読み取りと無進捗タイムアウト、中断時の `.tmp.<pid>` 掃除、および `operation` 分岐によるオフラインバックアップ操作 (保存・世界復元・ペルソナ記憶復元) + 失敗記録。
- `api/routes/system.py` / `api/routes/backups.py` / `saiverse/startup_alerts` 周り: バックアップ管理 API と detached worker 起動、失敗バナー通知。
- `frontend/src/components/GlobalSettingsModal.tsx` + `frontend/src/components/settings/BackupManagementPanel.tsx` + `frontend/src/i18n/messages.json` + 再起動待ちバナー: 「バックアップ」タブ UI と日英ローカライズ。
- ドキュメント: `docs/intent/version_aware_world_and_persona.md` (本ドキュメントへのリンク)、`docs/issues/snapshot_timeout_is_fixed_while_world_grows.md` (完了後 archive)、`docs/reference/api-endpoints.md` (自動生成再実行)、`docs/reference/scripts.md`。

---

## 5. 残るリスク・トレードオフ (正直に)

- **共有オブジェクトの物理破損 (単一障害点)**: 複数世代で共有する `.store/objects/<hash>` がディスク障害そのもので破損すると、それを共有する全世代でその 1 ファイルが復元エラーになる。ディスク故障対策は別途外部バックアップで行う前提とする。
- **画面が開けないときの復旧**: バックエンド自体が壊れて起動しない場合は画面の「バックアップ」タブを開けないため、緊急時用に `snapshot.bat` / `snapshot.sh` の CLI 経路も必ず残す。
- **ペルソナ単体復元時の建物ログとの差**: 1 人の `memory.db` だけを戻しても、`saiverse.db` の `building_messages` (部屋の会話履歴) 自体は戻らない。確認ダイアログにその挙動を明記する。
- **初回 (CAS 移行直後の 1 回目) のみ全件書き込み**: 初回だけ `.store/` を構築するため全対象ファイル (~10 GB) を書く (並列 + `zlib(1)` + 既圧縮 `.raw` で数十秒の見込み)。2 回目以降は変更分のみ。

---

## 6. 検証計画 (隔離 `SAIVERSE_HOME` + 実機確認)

1. **自動テスト (隔離 `tmp_path`)**:
   - CAS (format v3) の `save` / `restore` 往復・オブジェクト再利用・複数世代巻き戻し・`delete` (+ 自動 GC)・手動マニフェスト削除後の `gc`・破損マニフェスト時の fail-closed GC・format v2 `.zip` との共存・`export`。
   - `update_engine.py`: 進捗を出し続ける処理は完走し、無出力ハングだけが無進捗タイムアウトで停止 + `.tmp` 掃除されること。`operation` 分岐 (`snapshot_save`, `snapshot_restore`, `persona_memory_restore`) の成功・失敗記録と再起動経路。
   - `database/backup.py`: 標準 DB のバックアップが `backups/saiverse_db/` に作られ、`.bak-shm` を残さず、既存 `user_data/database/*.bak*` が安全に移行され、スナップショットに含まれないこと。
   - `sai_memory/backup.py`: ペルソナ単体 `memory.db` 復元の整合性検査・`pre_restore` 自動退避・原子入れ替え・失敗時ロールバック・`session_head_snapshot` 削除。
   - `api/routes/backups.py`: 一覧・即時バックアップ・削除・GC・事前検証エラー (存在しない名前/ペルソナ、パストラバーサル拒否)。
   - フロントエンド i18n 整合検査 (`npm test`) と型/ビルド検査 (`npm run build` / `tsc`)。
2. **実機検証 (まはーと確認)**:
   - グローバル設定 →「バックアップ」タブで世界スナップショットとペルソナ記憶バックアップの表示・作成・削除・復元が自然に通ること、およびアップデート時のスナップショット作成時間が 8〜12 分から数十秒へ縮むことを確かめる。

---

## 7. 経緯

- 2026-11-28 (PR #525〜#527、まはー実機確認): develop 切替時の初回 CAS スナップショット (3,795 ファイル / 非圧縮 9.87 GB) が **24.8 秒**、バックアップタブからの 2 回目 (3,831 ファイル) が **11.7 秒** で完了し、2 世代合計の CAS 倉庫実容量は **6.22 GB**（旧 ZIP 1 世代 6.57 GB より小さい）を確認。ライトモードの `<select>` / モーダル背景のトークン修正 (#526) と、上部サマリーへの CAS 倉庫容量・最新世代元データ表示および各世代の削除時解放容量 (`unique_bytes`) 表示 (#527) まで完了し、v0.3.36 の範囲に入った。固定タイムアウトの issue は `docs/issues/archive/` へ移動。台帳にあった旧行:

| 🟣 検証待ち | 世界スナップショットの CAS 高速化・進捗監視・グローバル設定「バックアップ」タブ | CAS 倉庫化、無進捗タイムアウト、DB 自動バックアップ分離、バックアップタブ UI (世界スナップショットとペルソナ記憶の個別復元) の実装と自動検査まで完了。次 = まはーが実機でバックアップタブの操作とスナップショット所要時間を確認する。 | まはー (実機確認) | [intent](../intent/world_snapshot.md) / [issue](../issues/snapshot_timeout_is_fixed_while_world_grows.md) | 2026-11-28 |
