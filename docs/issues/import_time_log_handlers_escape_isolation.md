# 一部の組み込みツールが、読み込まれた瞬間に自前のログファイルを開く

**起票**: 2026-09-11 (アイテム表示上限の隔離検証の途中で発覚)
**状態**: 検証待ち — 実装・隔離回帰済みで develop に取り込み済み。実機のログでの確認待ち

## 現象 (修正前)

`builtin_data/tools/` の `calculator.py` / `read_url_content.py` / `send_email_to_user.py` は、修正前にはモジュールが import された瞬間に、環境変数 `SAIVERSE_LOG_PATH` の先へ自前のログの口 (FileHandler) を開いて touch する。calculator は初期化の一行も書く。ツールのオートディスカバリは全ツールを読み込むので、この 3 本を使わなくても必ず発火する。

実害 (2026-09-11): 隔離環境 (`SAIVERSE_HOME` を一時フォルダへ) で検証スクリプトを走らせたとき、この変数だけが本番の `~/.saiverse/log.txt` を指したままで、本番ログに `[INFO] calculator logger initialized` が 5 行追記された (まはー裁定: 行はそのまま放置でよい)。ペルソナの記憶・DB には触れていない。

## なぜこの形が問題か

- 本体のログは session ごとのフォルダ (`~/.saiverse/user_data/logs/<session>/`) に移ったのに、この 3 本だけが古い置き場に書き続けている — `~/.saiverse/log.txt` が今も存在する理由はほぼこれだけ。
- 「読み込み = 副作用」は隔離を破る。`SAIVERSE_HOME` を倒しても、この変数を知らなければ本番へ書く。

## 対応方針

1. 3 本の自前 FileHandler を撤去し、普通の `logging.getLogger(__name__)` に合流させる (本体のログ設定が置き場を決める)。`SAIVERSE_LOG_PATH` の参照ごと消す。
2. あわせて `docs/test_environment.md` の隔離の作法に「環境変数の隔離は SAIVERSE_HOME だけでは足りない場合がある」旨を追記 (同日の変更で実施済み)。

## 関連

- [room_item_display_cap.md](../intent/room_item_display_cap.md) — 発覚の場 (検証の手順の隔離実行)

## 実装と検証 (2026-10-02)

- 3 本の `SAIVERSE_LOG_PATH`、親ディレクトリ作成・touch、FileHandler、レベル固定、伝播停止、calculator の初期化ログを撤去。通常の `logging.getLogger(__name__)` だけを残し、本体が設定する root handler へ合流させた。責任境界は [tool_logging.md](../intent/tool_logging.md)。
- 修正前再現: 3 本の個別 import と実際の `tools` 自動検出の 4 ケースすべてで、ツールを実行する前に隔離先の外を模した一時ログの内容または更新時刻が変わり、回帰テストが失敗した。本番ファイルは一切使用していない。
- `tests/test_tool_logging_isolation.py` は、個別 import / 自動検出 × `SAIVERSE_LOG_PATH` 指定 / 未指定 × ファイル既存 / 未作成の 16 条件を新規 subprocess で検査。旧ログと作業ディレクトリの既定ログの内容・更新時刻・未作成状態を保ち、本体の `configure_logging("DEBUG")` が設定した `backend.log` に DEBUG / INFO が一度ずつ届くことを確認する。
- URL 読み取りの取得引数・HTML 整形・返り値・タイムアウト、メールの宛先・From・件名・本文・TLS・SMTP エラーを fake HTTP / SMTP / DB で検査。計算は既存の四則演算・階乗・累乗・登録の回帰を使用。実 HTTP、実メール送信、ペルソナ、LLM は動かしていない。
- 検証結果: 対象回帰と既存 calculator は計 24 件合格。変更 Python の `ruff check`、`scripts/check_in_flight.py`、`git diff --check` も合格。`gen_reference_docs.py` を隔離環境で再生成し、API / DB の差分なしを確認。ツールカタログは checkout に無いアドオン 50 本だけが落ちる既存の環境差 (124 → 74) が出たが、HEAD の修正前 3 モジュールでも同じ 74 本の出力と完全一致したため、その無関係な削除差分は採用していない。
- 次は PR レビューと develop への取り込み判断。既存のログや起動中プロセスは変更せず、issue は未解決フォルダに置いたままにする。

## 経緯

- 2026-09-11: アイテム表示上限の隔離検証で発覚。既存ログの追記はそのまま残す裁定、3 本を本体ログへ合流させる対応方針を確定。`docs/test_environment.md` に変数の隔離を注意書きした。
- 2026-10-02: 対応方針確定・着手待ちから実装と隔離検証へ進めた。完了・マージ扱いにはせず、PR レビュー待ち。


## 追加レビュー検証 (2026-10-03)

`read_url_content` の開始・完了ログから userinfo・query・fragment を除去。不正 URL は raw 値を残さない。4 件の赤テストを修正し、logging / calculator **28 passed**、ruff 合格。HTTP は fake、取得と返却の URL は元の仕様を維持。

- 2026-10-03: PR #351 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳から移送した旧次アクション: 「独自の保存先を廃し、本体の root logger に合流する実装は PR レビュー待ち。次 = 隔離回帰と差分のレビュー → develop への取り込み判断。」(誰待ち: レビュー待ち)
