# Git が SAIVerse のフォルダの中にしか無い PC では、Web 検索の部品 (SearXNG) が起動しない

**状態**: ✅ 完了 — 2026-10-02 に発見し、同日に直した (PR #344)。直し方が効くことは、まはーが Windows サンドボックスで手で確かめた。直した後の起動スクリプトが自動でファイルを置くところは、本物の環境では通していない (テストでは確かめてある)。再発したら起案し直す
**深刻度**: P2 — 該当する PC では Web 検索が使えない。SAIVerse 本体は動く。該当するのは、Git が入っていない Windows に ZIP から導入した利用者のうち、winget が使えず、setup.bat が Git をフォルダの中 (`.git-portable`) に入れた人

## 何が起きるか (2026-10-02、まはーが Windows サンドボックスで v0.3.19 の ZIP を導入して確認)

start.bat が開く「SearXNG」のウィンドウに、次のエラーが出て止まる。

```
File "...\scripts\.searxng-src\searx\version.py", line 109, in <module>
    vf = importlib.import_module('searx.version_frozen')
ModuleNotFoundError: No module named 'searx.version_frozen'

During handling of the above exception, another exception occurred:
...
File "...\scripts\.searxng-src\searx\version.py", line 69, in get_git_version
    git_commit_date_hash: str = subprocess_run(r"git show -s --date='format:%Y.%m.%d' --format='%cd+%h'")
...
FileNotFoundError: [WinError 2] 指定されたファイルが見つかりません。
```

## 原因 (確定)

SearXNG は起動のとき、自分のバージョンの番号を知るために `git` コマンドを呼ぶ (`searx/version.py`)。バージョンの番号を書いたファイル `searx/version_frozen.py` があれば `git` を呼ばないが、SAIVerse が取ってくる SearXNG のソースにはこのファイルが無い。

setup.bat は、Git が無い PC では Git をフォルダの中 (`.git-portable\cmd`) に入れる。setup.bat 自身と更新プログラムは、その場所を PATH に足して使う。start.bat は Node.js については同じ手当てをしている (`if exist ".node\node.exe" set "PATH=%CD%\.node;%PATH%"`) が、Git については何もしていない。だから SearXNG のウィンドウからは `git` が見つからない。

SearXNG のコードは、`git` が失敗したとき (`CalledProcessError`) は握って既定のバージョンの番号で続けるが、`git` そのものが見つからないとき (`FileNotFoundError`) は握らないので、起動ごと落ちる。

## 直したこと (2026-10-02)

SearXNG を起動する直前 (`scripts/run_searxng_server.ps1` / `.sh`) に `scripts/ensure_searxng_version.py` を呼ぶ。`git` が見つからないときだけ、`searx/version_frozen.py` を書く。書く中身は、SearXNG 自身が `git` の失敗時に使う既定の値と同じ。`git` が見つかるときは何も書かないので、SearXNG はこれまでどおり本当のバージョンの番号を名乗る。起動のたびに確かめるので、すでに導入済みのフォルダも、更新すれば直る。

start.bat でフォルダの中の Git を PATH に足す案は採らなかった。Git がどこにも無い PC (Git の自動導入に失敗した PC) では、それでも落ちるため。

### 確かめたこと

- まはーが Windows サンドボックスで、`searx/version_frozen.py` を手で置いて SearXNG を起動し直し、`Serving Flask app 'webapp'` まで進むことを確かめた (置く前は `FileNotFoundError` で落ちていた)。
- テスト (`tests/test_clean_windows_startup.py`): `git` が見つからないときに書く。書いた中身に、SearXNG が読む 5 つの名前がある / `git` が見つかるときは書かない / すでにあるファイルには触らない / ソースのフォルダが無くても起動スクリプトを止めない / 両方の起動スクリプトが、SearXNG を起動する前にこれを呼ぶ。

### 確かめていないこと

- 直した後の起動スクリプトを、Git がフォルダの中にしか無い PC で最初から通すこと (手で置いたファイルで効くことは確かめたが、スクリプトが自動で置くところは通していない)。

### 同じ画面で見えた別件 (直していない)

SearXNG の起動時に、検索先の一つ bilibili が `ModuleNotFoundError: No module named 'tzdata'` で読み込めないと出る。Windows には時刻帯のデータが無く、Python は `tzdata` という部品からそれを読むが、SearXNG 用の仮想環境にこの部品が入っていない。開発機の SearXNG の仮想環境にも入っていなかったので、まっさらな Windows に限らず、Windows ではどこでも起きている。ほかの検索先は動く。

## 検討した直し方

- SearXNG のソースを用意するとき (`scripts/setup_searxng.ps1` / `.sh`。Git があれば clone、無ければ `scripts/download_searxng_source.py` が ZIP を取る) に、`searx/version_frozen.py` を書いておく。SearXNG が起動のときに `git` を呼ばなくなるので、Git がどこにあるかに関係なく起動する。
- start.bat で、フォルダの中の Git を PATH に足す (Node.js と同じ形)。こちらは `git` を呼ぶこと自体は残る。

## 関連

- [git_required_for_zip_install.md](../git_required_for_zip_install.md) — このテストの本体
- [clean_windows_missing_vc_runtime_blocks_startup.md](clean_windows_missing_vc_runtime_blocks_startup.md) — 同じテストで見つかった、本体が起動しない件
