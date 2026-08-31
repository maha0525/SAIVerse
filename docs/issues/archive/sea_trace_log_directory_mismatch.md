# Issue: `sea_trace.log` だけ別タイムスタンプディレクトリに作られる

**ステータス**: ✅ 解決済み (2026-07-19、構造的集約)
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `saiverse/logging_config.py`, `~/.saiverse/user_data/logs/{YYYYMMDD_HHMMSS}/`

## 背景

セッションログは `~/.saiverse/user_data/logs/{YYYYMMDD_HHMMSS}/` 以下に 4 ファイル (`backend.log`, `llm_io.log`, `sea_trace.log`, `timeout_diagnostics.log`) が作られる想定だが、`sea_trace.log` だけ他 3 ファイルと別のタイムスタンプディレクトリに分離されることがある。

体感では「2 回目以降の起動で発生する」印象があるが未検証。SEA トレースを追うときに該当セッションのディレクトリを取り違えて混乱する。

## 確認事項

1. ロガー初期化のタイミング — `saiverse.sea_trace` ロガーのファイルハンドラが他ロガーと別のセッション ID/タイムスタンプを参照していないか
2. 初回起動 vs 2 回目以降で再現性はあるか
3. `getLogger("saiverse.sea_trace")` のハンドラ取り付けが、セッションディレクトリ確定前に走っていないか (import 時など)

## 解決案候補

- セッションディレクトリ生成と全ロガーのハンドラ取り付けを 1 か所に集約し、再入で別ディレクトリが選ばれないようにする
- `sea_trace` ロガー初期化を遅延させ、他ロガーと同じセッション ID を共有

## 関連リソース

- `saiverse/logging_setup.py` (該当しそう)
- メモリ: Logging Structure (2026-02-08)

## ログ

- 2026-05-09: issue 起票。再現条件 (2 回目以降?) を含めて要調査。
- 2026-07-19: **解決 (解決案候補の「1 か所に集約」を採用)**。真因の調査結果 (事実): 実ファイルは `saiverse/logging_setup.py` ではなく `saiverse/logging_config.py`。`sea_trace` と `timeout_diagnostics` の 2 ロガーだけが **遅延初期化** (初回 `get_*_logger()` 使用時に `_configure_*` を呼ぶ) で、他 3 ファイル (backend / llm_io / error) は `configure_logging()` で起動時に一括生成されていた。`get_session_log_dir()` はモジュールグローバル `_session_log_dir` で memoize されるため **同一プロセス内では分離しない** (memoize 済みの同じディレクトリを返す)。したがって「別ディレクトリに分かれる」症状が起きうるのは、遅延初期化ロガーが `configure_logging()` を通っていない別プロセス (script 実行等) で初めて叩かれ、その場で自前の新しいタイムスタンプディレクトリを作る場合 (推測: 実機での 2 回目以降という体感の裏取りは未再現)。修正: `configure_logging()` 内で `_configure_sea_trace_logger()` / `_configure_timeout_diagnostics_logger()` を **起動時に eager 実行** し、5 ファイルすべてを同一セッションディレクトリで同時生成するように集約 (遅延 getter は未設定使用へのフォールバックとして残置)。隔離 `SAIVERSE_USER_DATA_DIR` で `configure_logging()` 直後に 5 ファイル全てが同一ディレクトリに生成されることを実証済み。**未再現の断り**: 「別ディレクトリに分離する具体的な発生パスそのもの」は再現できていない。本修正は遅延初期化というタイミング依存経路を排除して同一プロセス内での分離余地をゼロにするもので、体感された症状の根が別プロセス起因である場合はそのプロセス側が `configure_logging()` を呼べば同様に解消する。
