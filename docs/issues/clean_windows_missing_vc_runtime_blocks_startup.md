# Visual C++ の部品が入っていない Windows では、setup が最後まで通っても SAIVerse が起動しない

**状態**: 検証待ち (2026-10-02 発見、同日に直した。残るのは、部品が入っていない Windows で自動導入が通ることの確認)
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

## 直したこと (2026-10-02、まはー裁定「自動で入れるようにしよう」)

- **setup.bat が自動で入れる。** Node.js の確認のあとに `scripts/install_vc_redist.ps1` を呼ぶ。部品 (`msvcp140.dll` / `msvcp140_1.dll` / `vcruntime140.dll` / `vcruntime140_1.dll`) が Windows のフォルダに揃っていれば何もしない。足りなければ、Microsoft の配布元からインストーラーを取り、Microsoft の署名つきであることを確かめてから実行する。途中で Windows が「このアプリが変更を加えることを許可しますか」と確認する。入れられなかったとき (ダウンロードの失敗、確認を断った) は、手で入れる場所を知らせて、setup は先へ進む。
- **部品が無いまま起動したときは、読める案内を出して止まる。** `main.py` が、記憶の検索の部品を読み込む前に確かめる (`saiverse/windows_runtime_check.py`)。部品が見つからず、onnxruntime も実際に読み込めないときだけ、日本語と英語で「この部品が必要。setup.bat をもう一度実行するか、Microsoft の配布元から入れる」と出して終了する。部品の有無の見立てだけでは止めない (見立てが外れていて onnxruntime が読み込める PC を、起動できなくしないため)。
- macOS / Linux には関係しない (この部品は Windows のもの)。

### 確かめたこと

- テスト (`tests/test_clean_windows_startup.py`): 足りない部品の名前が出る / Windows 以外では何も言わない / onnxruntime が読み込めないときは案内を出して終了コード 1 で止まる / 何も足りなければ素通りする / 見立てが外れていても onnxruntime が読み込めれば止めない / 日本語を表示できないコンソールでも案内が落ちない / `main.py` が記憶の検索の部品より前に確かめる / setup.bat が導入スクリプトを呼ぶ / .bat と .ps1 が ASCII のまま。
- 部品が入っている開発機で導入スクリプトを実行し、「入っている」とだけ答えて何もしないことを確かめた。起動時の検査が素通りすることも確かめた。
- 導入スクリプトの構文検査 (PowerShell のパーサー) と、署名を確かめる条件式が Microsoft 署名のファイルで正しく働くことを確かめた。

### レビュー (2026-10-02、ローカルのレビューモデルで一巡)

指摘の裏を取った。

- 採用: (1) 32 ビットの画面から呼ばれると、64 ビットの Windows なのに「x64 ではない」と判定し、Windows のフォルダも 32 ビット側を見てしまう。本当の種類と本当のフォルダを見るようにした (32 ビットの PowerShell で実行して、「入っている」と正しく答えることを確かめた)。(2) ネットワークに繋がらない PC で、ダウンロードが何も表示せずに待ち続ける。60 秒で諦めて案内を出すようにした。(3) SearXNG に書く版の番号のテストが、値そのものを確かめていなかった。SearXNG 自身の既定の値と一致することを確かめる形にした。
- 採らなかった: 「32 ビットの Python だと、入れても直らないのに成功と出る」は、README が 64 ビットの Python を指定していて、onnxruntime も 32 ビットの Windows 用の配布をしていないので、この段に来る前に部品の導入で止まる。「入れたばかりの Windows では、正しい署名でも確認に失敗するかもしれない」は、推測で条件を緩めずに、サンドボックスで実際の結果を見てから決める (失敗したときは、インストーラーを実行せずに状態を表示して、手で入れる場所を知らせる)。
- 既知の穴として残す: 導入スクリプト (PowerShell) の判断の部分には自動テストが無い。部品が入っていない Windows でしか通せないので、サンドボックスでの実行が検証になる。

### 確かめていないこと

- 部品が入っていない Windows で、導入スクリプトがインストーラーを取って実行し、部品が入るところ。開発機には部品が入っているので通せない。新しく開いた Windows サンドボックスで、導入スクリプトだけを実行して確かめる (Python は要らない)。

## 直した後の確かめ方

部品の自動導入は、この部品が入っていない Windows でしか確かめられない。Windows サンドボックスを新しく開けば、その状態になる。導入の処理だけなら Python を入れる必要が無いので、サンドボックスを開いてその処理を一つ実行し、`where msvcp140.dll` で確かめれば足りる。

## 関連

- [git_required_for_zip_install.md](git_required_for_zip_install.md) — このテストの本体 (ZIP から導入した利用者への Git の自動導入)
- [searxng_needs_git_on_path.md](searxng_needs_git_on_path.md) — 同じテストで見つかった、Web 検索の部品が起動しない件
