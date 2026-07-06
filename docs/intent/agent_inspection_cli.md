# Intent: エージェント検分 CLI (`scripts/inspect_world.py`)

- **Status**: v0.1 (2026-07-06 起草)
- **Owner**: まはー / 実装・主利用者はエア (Claude Code 等の AI エージェント)
- **関連**: `docs/test_environment.md`, `docs/intent/autonomous_behavior_v2.md` §12 (一日シミュレータ),
  メモリ `project_sandbox_persona_testing`

## 1. なぜ作るか

SAIVerse の不具合調査・挙動検証のたびに、AI エージェントが sqlite クエリや
ログの grep をその場でゼロから組み立てている。memory.db のスキーマ確認、
タグの JSON 構造の思い出し、cp932 での文字化け回避 — 毎回同じ足場を
組み直すのは時間の無駄で、しかも組み間違えると誤った観測に基づく誤診に繋がる。

この CLI は「SAIVerse の今の状態を読む」ための統一入口を提供する。
対象はペルソナの記憶 (memory.db)、世界の状態 (saiverse.db)、実行ログ
(logs/)。**エージェントが第一の利用者**であり、出力はエージェントが
そのまま読解・引用できる形式を最優先する。

副次目的として、まはーの手動観測 (UI を開いて確認する等) への依存を減らす。
サンドボックス基盤 (clone → 一日シム → 一日新聞) の「観測面」を担う部品。

## 2. 不変条件 (INVARIANTS)

1. **完全読み取り専用**。SQLite は `mode=ro` の URI で開く。対象環境に
   ファイルもディレクトリも作らない。
   - `SAIMemoryAdapter` / `MemoryCore` を**経由しない**理由: adapter の
     `__init__` は起動時バックアップとスキーマ migration (ADD COLUMN) を
     走らせる = 検分のつもりが対象を書き換える。生 sqlite3 で読む。
   - SQLAlchemy の ORM も使わない (event listener / メタデータ更新の
     経路を持ち込まないため)。スキーマは `database/models.py` と
     `sai_memory/memory/storage.py` を正とし、必要な列だけ SELECT する。
2. **stdout は UTF-8 固定** (`sys.stdout.reconfigure`)。Windows コンソール
   (cp932) で日本語が化けない・落ちない。
3. **切り詰めがデフォルト**。メッセージ本文・LLM I/O は既定でプレビュー長に
   切り詰め、`--full` で全文。エージェントのコンテキストを浪費しない。
4. **存在しないものは正直にエラー**。ペルソナ不在・DB 不在・セッション不在は
   推測で補完せず、パスを添えて明示的に失敗する。

## 3. 対象環境の選択

| 指定 | 解決 |
|---|---|
| (無指定) | 本番: `database.paths.default_db_path()` + `saiverse.data_paths.get_saiverse_home()` |
| `--env test` | `test_data/.saiverse` + `test_data/user_data/database/saiverse.db` (リポジトリ相対) |
| `--home <path>` / `--db <path>` | 任意 (個別上書き。`--env` より優先) |

ログディレクトリは user_data (DB パスの 2 つ上、`database/` レイアウトの場合)
配下の `logs/` を使う。

## 4. サブコマンド

| コマンド | 読む場所 | 出すもの |
|---|---|---|
| `personas` | saiverse.db (ai + occupancy + building) | ペルソナ一覧: ID / 名前 / 活動状態 / モデル / 現在位置 |
| `memory <persona>` | memory.db (messages) | メッセージ (フィルタ: `--tags` `--thread` `--line-role` `--scope` `--track` `--pulse` `--role` `--grep` `--since` `--date`、`--tail N` 既定 20) |
| `threads <persona>` | memory.db (threads + messages) | スレッド一覧: 件数 / 期間 / overview 冒頭 |
| `tracks <persona>` | saiverse.db (action_track) | Track 一覧 (既定は未終了のみ、`--all` で全部) |
| `tasks <persona>` | saiverse.db (persona_task) | タスク/欲求一覧 (`--desires` で欲求のみ、`--status` フィルタ) |
| `day-plan <persona>` | saiverse.db (persona_day_plan) | 時間割 (既定は最新日、`--date`)。コマの status / skip_reason を含む |
| `sessions` | logs/ | セッションログディレクトリ一覧 (新しい順) |
| `llm-io` | logs/<session>/llm_io.log | LLM I/O (フィルタ: `--persona` `--node` `--type` `--grep`、`--session latest` 既定) |
| `errors` | logs/<session>/error.log (+ timeout_diagnostics.log) | WARNING 以上のダイジェスト: ロガー別件数 + 直近の実体 |

全サブコマンドに `--json` (機械可読出力) がある。

## 5. 名前について

`scripts/inspect.py` は不可 — `python scripts/inspect.py` 実行時に `scripts/`
が `sys.path[0]` に入り、標準ライブラリの `inspect` (dataclasses 等が内部
import する) を覆い隠して壊れる。`inspect_world.py` はこの衝突を避けた名前。

## 6. スコープ外 (non-goals)

- 書き込み・修復操作 (それは別ツールの仕事。この CLI に足さない)
- semantic recall (embedder のロードが必要。Memory Settings UI / recall ツールの領分)
- リアルタイム監視 (tail -f 相当)。必要になったら別途
- 本番/テスト以外の第三環境のプリセット

## 7. 将来の拡張候補

- 一日シム生データ抽出 (`run_day_sim.py --raw-log`) との出力形式共通化
- `errors --watch` 的な継続監視 (活性化配線後の自律稼働を見守る用途)
- Memopedia / Chronicle (arasuji_entries) の検分サブコマンド
