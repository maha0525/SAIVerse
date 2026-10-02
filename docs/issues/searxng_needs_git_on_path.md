# Git が SAIVerse のフォルダの中にしか無い PC では、Web 検索の部品 (SearXNG) が起動しない

**状態**: 未着手 (2026-10-02 発見、原因は確定)
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

SearXNG は起動のとき、自分の版の番号を知るために `git` コマンドを呼ぶ (`searx/version.py`)。版の番号を書いたファイル `searx/version_frozen.py` があれば `git` を呼ばないが、SAIVerse が取ってくる SearXNG のソースにはこのファイルが無い。

setup.bat は、Git が無い PC では Git をフォルダの中 (`.git-portable\cmd`) に入れる。setup.bat 自身と更新プログラムは、その場所を PATH に足して使う。start.bat は Node.js については同じ手当てをしている (`if exist ".node\node.exe" set "PATH=%CD%\.node;%PATH%"`) が、Git については何もしていない。だから SearXNG のウィンドウからは `git` が見つからない。

SearXNG のコードは、`git` が失敗したとき (`CalledProcessError`) は握って既定の版の番号で続けるが、`git` そのものが見つからないとき (`FileNotFoundError`) は握らないので、起動ごと落ちる。

## 直し方の候補

- SearXNG のソースを用意するとき (`scripts/setup_searxng.ps1` / `.sh`。Git があれば clone、無ければ `scripts/download_searxng_source.py` が ZIP を取る) に、`searx/version_frozen.py` を書いておく。SearXNG が起動のときに `git` を呼ばなくなるので、Git がどこにあるかに関係なく起動する。
- start.bat で、フォルダの中の Git を PATH に足す (Node.js と同じ形)。こちらは `git` を呼ぶこと自体は残る。

## 関連

- [git_required_for_zip_install.md](git_required_for_zip_install.md) — このテストの本体
- [clean_windows_missing_vc_runtime_blocks_startup.md](clean_windows_missing_vc_runtime_blocks_startup.md) — 同じテストで見つかった、本体が起動しない件
