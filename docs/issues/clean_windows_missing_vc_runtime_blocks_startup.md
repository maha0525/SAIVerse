# Visual C++ の部品が入っていない Windows では、setup が最後まで通っても SAIVerse が起動しない

**状態**: 未着手 (2026-10-02 発見、原因は確定。直し方はまはーの裁定待ち)
**深刻度**: P1 — 該当する PC では、start.bat を実行してもバックエンドが起動直後に落ち、画面が開かない。買ったばかりの PC や、入れ直した直後の Windows が該当しうる。setup.bat は何も言わずに「Setup Complete!」まで進むので、利用者には原因が分からない

## 何が起きるか (2026-10-02、まはーが Windows サンドボックスで v0.3.19 の ZIP を導入して確認)

1. setup.bat は最後まで通る。
2. start.bat を実行すると、バックエンドのウィンドウに次のエラーが出て、SAIVerse が起動しない。

   ```
   File "...\sai_memory\memory\recall.py", line 11, in <module>
       from fastembed import TextEmbedding
   ...
   File "...\onnxruntime\capi\_pybind_state.py", line 32, in <module>
       from .onnxruntime_pybind11_state import *  # noqa
   ImportError: DLL load failed while importing onnxruntime_pybind11_state: 指定されたモジュールが見つかりません。
   ```

   その前に、同じエラーで道具が 2 つ読み込めない警告も出る (`memory_search_brief.py` と `thread_switch.py`)。

## 原因 (確定)

記憶の検索に使っている部品 onnxruntime は、Microsoft の「Visual C++ 再頒布可能パッケージ」の部品を必要とする。開発機の onnxruntime 1.23.2 で、`onnxruntime_pybind11_state.pyd` と `onnxruntime.dll` が読み込む部品を調べると、`MSVCP140.dll` / `MSVCP140_1.dll` / `VCRUNTIME140.dll` / `VCRUNTIME140_1.dll` が含まれていた。

この部品は Windows に最初から入っているものではない。ゲームやほかのアプリを入れたときに一緒に入ることが多いので、ふつうの PC にはたまたま入っている。

確かめた順:

- サンドボックスで `where msvcp140.dll` を実行すると、「与えられたパターンのファイルが見つかりませんでした」と出た。
- Microsoft の配布元 (`https://aka.ms/vs/17/release/vc_redist.x64.exe`、Microsoft Visual C++ 2015-2022 Redistributable (x64) 14.44.35211) を入れてから start.bat を実行し直すと、道具は 74 個全部読み込まれ、バックエンドが起動し、初回のセットアップ画面が開いた。

## 決めること

- setup.bat が、この部品が無いときに自動で入れるか。Node.js と Git は、すでに setup.bat が自動で入れている (winget、無理なら予備の道)。この部品のインストーラーは、管理者の許可を求める画面 (UAC) を出す。
- 自動で入れない場合、または入れられなかった場合に、利用者へどう伝えるか。いまは Python のエラーの長い表示が出るだけなので、起動のときに「この部品が必要です。ここから入れてください」と読める形で知らせる必要がある。
- macOS / Linux には関係しない (この部品は Windows のもの)。

## 直した後の確かめ方

部品の自動導入は、この部品が入っていない Windows でしか確かめられない。Windows サンドボックスを新しく開けば、その状態になる。導入の処理だけなら Python を入れる必要が無いので、サンドボックスを開いてその処理を一つ実行し、`where msvcp140.dll` で確かめれば足りる。

## 関連

- [git_required_for_zip_install.md](git_required_for_zip_install.md) — このテストの本体 (ZIP から導入した利用者への Git の自動導入)
- [searxng_needs_git_on_path.md](searxng_needs_git_on_path.md) — 同じテストで見つかった、Web 検索の部品が起動しない件
