# アドオン専用の Python 環境が、基にした Python のフォルダごと消える更新を検知できない

**状態**: 未解決 (実害の報告なし。2026-10-22 のローカル LLM レビューの指摘から起こした)

## 症状

アドオン専用の Python 環境 (`~/.saiverse/addon_install/<addon_id>/envs/<name>/`) は venv で、
中の `pyvenv.cfg` の `home =` が、作ったときの基の Python のフォルダを指している。
基の Python が **フォルダごと消える形** で更新されると (pyenv / uv / Microsoft Store の
ようにバージョンごとのフォルダへ入れる導入で、古い版を消したとき)、venv は起動できなく
なるのに、SAIVerse の照合はそれを検知しない:

- `saiverse/addon_paths.py` の `addon_env_matches_current_python` は記録の major.minor と
  python 実行ファイルの存在しか見ない。壊れた venv の python (スタブ) はファイルとしては
  存在するので照合が通る。
- 結果、voice-tts の音声合成の子プロセスが起動のたびに失敗し、「入れ直して」という案内に
  たどり着かない。

python.org の導入ファイル (README が案内している経路) は同じフォルダ (`Python313` など) を
上書きするので、この症状は起きない。

## 経緯

2026-10-22 に照合を完全一致から major.minor の一致へ緩めた
([dependency_management.md](../intent/dependency_management.md) §7 の同日の項)。完全一致の
時代は、Python のパッチ更新のたびに環境を作り直していたので、この症状は偶然回復していた
(作り直し自体が数 GB のダウンロードを伴う過剰な手当てだった)。緩和でその偶然の回復が
消え、潜在していたこの穴が残った。同じ穴は SAIVerse 本体の `.venv` にも前からある
(基の Python が消えても何も検知しない)。

## 直し方の案

`pyvenv.cfg` の `home =` のフォルダが実在するかを、`addon_env_matches_current_python` の
照合に足す (実在しなければ作り直し対象)。テストの環境の偽装 (`_write_env` と
`_fake_run_creating_venv`) に `pyvenv.cfg` を書く手当てが一緒に要る。

## 回復手段 (直すまで)

アドオン管理からアドオンを入れ直す (専用の環境が作り直される)。
