# 一部の組み込みツールが、読み込まれた瞬間に自前のログファイルを開く

**起票**: 2026-09-11 (アイテム表示上限の隔離検証の途中で発覚)
**状態**: 未解決 — 対応方針は決まっている (下)、着手待ち

## 現象

`builtin_data/tools/` の `calculator.py` / `read_url_content.py` / `send_email_to_user.py` は、モジュールが import された瞬間に、環境変数 `SAIVERSE_LOG_PATH` の先へ自前のログの口 (FileHandler) を開いて一行書く。ツールのオートディスカバリは全ツールを読み込むので、この 3 本を使わなくても必ず発火する。

実害 (2026-09-11): 隔離環境 (`SAIVERSE_HOME` を一時フォルダへ) で検証スクリプトを走らせたとき、この変数だけが本番の `~/.saiverse/log.txt` を指したままで、本番ログに `[INFO] calculator logger initialized` が 5 行追記された (まはー裁定: 行はそのまま放置でよい)。ペルソナの記憶・DB には触れていない。

## なぜこの形が問題か

- 本体のログは session ごとのフォルダ (`~/.saiverse/user_data/logs/<session>/`) に移ったのに、この 3 本だけが古い置き場に書き続けている — `~/.saiverse/log.txt` が今も存在する理由はほぼこれだけ。
- 「読み込み = 副作用」は隔離を破る。`SAIVERSE_HOME` を倒しても、この変数を知らなければ本番へ書く。

## 対応方針

1. 3 本の自前 FileHandler を撤去し、普通の `logging.getLogger(__name__)` に合流させる (本体のログ設定が置き場を決める)。`SAIVERSE_LOG_PATH` の参照ごと消す。
2. あわせて `docs/test_environment.md` の隔離の作法に「環境変数の隔離は SAIVERSE_HOME だけでは足りない場合がある」旨を追記 (同日の変更で実施済み)。

## 関連

- [room_item_display_cap.md](../intent/room_item_display_cap.md) — 発覚の場 (検証の手順の隔離実行)
