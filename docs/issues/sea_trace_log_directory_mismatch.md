# Issue: `sea_trace.log` だけ別タイムスタンプディレクトリに作られる

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `saiverse/logging_setup.py` (推定), `~/.saiverse/user_data/logs/{YYYYMMDD_HHMMSS}/`

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
