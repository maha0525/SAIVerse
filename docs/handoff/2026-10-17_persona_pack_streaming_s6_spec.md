SAIVerse「ペルソナまるごとパック」のストリーミング化 (intent §3 原則 6) の束 S6 (最終): 一時置き場の移動・上限の整理・ピークメモリの退行テスト。設計調査 C:\Users\shuhe\AppData\Local\Temp\claude\C--Users-shuhe-workspace-SAIVerse\15d12ccd-3059-439e-8cdb-8ac1abe24213\scratchpad\streaming_survey.md の C-1 (置き場所と tmpfs の注意) と E-4 (上限の意味の変化) を**実装前に必ず読む**。

## 作業場所と禁止事項 (厳守)

- 作業ツリー: C:\Users\shuhe\workspace\SAIVerse\.claude\worktrees\persona-data-map (ブランチ feature/persona-archive)。**ここで直接作業。worktree 隔離・再委譲は禁止。**
- **触ってよいのは api/routes/people/persona_pack.py、saiverse_memory/persona_pack/importer.py、tests/test_persona_pack_import.py、tests/test_persona_pack_api.py、新規の tests/test_persona_pack_memory.py、docs/intent/persona_pack.md だけ。**
- **`git checkout --` / `git restore` / `git stash` / `git reset` / `git add` / `git commit` は禁止。**
- 本番データ (`~/.saiverse/`) への接触禁止。Python は `C:\Users\shuhe\workspace\SAIVerse\.venv\Scripts\python.exe`。**フルスイート禁止** — 回すのは上記のテスト 3 ファイル (`-n 0`)。
- ruff 緑。コメント・docstring は識別子と既存用語で書き、新しい日本語の呼び名 (造語) を作らない。

## 1. アップロードと書き出しの一時置き場を SAIVERSE_HOME 配下へ (api/routes/people/persona_pack.py)

- 現状 (調査 C-1): アップロードは `tempfile.NamedTemporaryFile(prefix="persona_pack_", suffix=".zip")`、書き出しは `tempfile.mkdtemp(prefix="persona_pack_export_")` で、どちらも **OS の一時フォルダ**。Linux の配布によっては /tmp が tmpfs (= RAM) で、2 GB の機械では最大 2 GiB のアップロードと、その隣に置かれる作業用 SQLite がメモリを食う。
- 直し方: `<SAIVERSE_HOME>/tmp/` の下に置く (フォルダが無ければ作る)。SAIVERSE_HOME の取り方は API の既存コード (saiverse_home をどう得ているか) を読んで同じ口を使う。`tempfile.NamedTemporaryFile(dir=...)` / `mkdtemp(dir=...)` で dir を渡すだけの形が良い。掃除 (finally の unlink / rmtree) は従来どおり。
- ペルソナフォルダ (`personas/<id>/`) の下には置かない (書き出しが拾ってしまうため)。`<SAIVERSE_HOME>/tmp` が書き出しの対象に入らないことを export.py の収集範囲を読んで確かめる (確かめた結果を報告に書く。入るなら場所を再考して報告)。
- プロセスが落ちて残ったファイルは次の取り込み・書き出しでは邪魔にならない名前 (tempfile が一意名を作る) なので、起動時の掃除は作らない。intent に「異常終了で `<SAIVERSE_HOME>/tmp` に残骸が残りうる (害は容量のみ)」と既知の限界で書く。

## 2. 上限の整理 (importer.py)

- **`MAX_ENTRY_UNCOMPRESSED_BYTES` と `MAX_JSON_TOTAL_BYTES` を廃止する** (定数・check_uncompressed_size の該当検査・参照テストごと)。根拠だった「丸ごとメモリに読む」が S2〜S4 で消えたため。残る上限は二本:
  - `MAX_UNCOMPRESSED_BYTES` (16 GiB、合計): ディスクの保護。コメントを付け直す — 展開されるファイルに加えて、配列の JSON と同程度の大きさの作業用 SQLite もディスクに載る旨。
  - `MAX_JSON_ELEMENT_CHARS` (64 Mi 文字、1 要素): メモリの保護 (要素の文字列と Python オブジェクトは同時にメモリに載る)。
- 参照しているテスト (tests/test_persona_pack_import.py の 1 エントリ上限・JSON 合計上限のテスト、tests/test_persona_pack_api.py に MAX_UNCOMPRESSED_BYTES を差し替えるテスト) を、残る二本の上限の検査に合わせて改訂・削除する。`check_uncompressed_size` の docstring も二本に合わせる。

## 3. ピークメモリの退行テスト (新規 tests/test_persona_pack_memory.py)

- 目的: 逐次化の後戻り (丸読み・全行リストの復活) を機械で止める。
- 形: 合成の大きな世界 (メッセージ数万件。1 件の content は短くてよいが、総 JSON が数十 MB になる規模) を tmp_path に作り、export → new import → merge import を回し、`tracemalloc` で各工程のピークを測る。
- 合格条件は「ピークがデータ総量に比例しない」こと。較正の仕方: 同じ形でメッセージ件数を 1/10 にした小さい世界のピークを測り、大きい世界のピークが「小さい世界のピーク + 十分な定数余裕」以内であることを確かめる (絶対値のしきい値はマシン差で割れるので、比で見る)。比の根拠をコメントに書く。
- 実行時間は 1 ファイル 90 秒以内に収める (件数は較正して決める)。tracemalloc は Python の確保だけを見る (SQLite の C 側は見えない) が、消した保持は全部 Python 側なのでこれでよい — この旨もコメントに。
- 世界の合成は tests/test_persona_pack_export.py の `_build_world` の流儀を参考に、必要なら軽量版をこのファイルに書く (export.py のフィクスチャを import してもよい)。

## 4. intent の仕上げ (docs/intent/persona_pack.md)

- §3 原則 6 に、実装の現況を一段落で足す: 書き出しは一行ずつの逐次書き (ChatGPT 互換は 1 スレッドぶんずつ)。取り込みは逐次読み + 作業用 SQLite。残っている保持は「件数が有界なもの (所持品・約束・スレッド数など) と、id だけの集合・射影 (本文を持たない)」で、ペルソナの寿命に比例して育つ丸ごとの保持は無い。既知の残余も書く: 書き出しの messages の ORDER BY created_at は索引が無く SQLite の外部ソートに逃げる (一時ファイルの置き場は OS の設定に依る) / 取り込み先側の id 集合 (合流時の照合など) は取り込み先の規模に比例するが id のみ。
- 上限の二本化 (§で上限に触れている箇所全部) と、一時置き場の移動 + 残骸の既知の限界を反映する。

## 検証と報告

- tests/test_persona_pack_import.py・tests/test_persona_pack_api.py・tests/test_persona_pack_memory.py を `-n 0` で緑に。ruff 緑。
- 報告: 一時置き場の実装 (SAIVERSE_HOME の取り方・書き出しの収集範囲の確認結果)、上限の整理の差分、メモリテストの較正値 (件数・比・実測のピーク)、intent の変更行、迷った点。
