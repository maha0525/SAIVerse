# voice-tts をアドオンカタログに載せる

**起票**: 2026-10-05 (アドオンカタログの intent の Phase 4-E から、この件だけを切り出した)
**状態**: 設計中。前提になるカタログの仕組み (導入時の質問と、アドオン専用の Python 環境) が、まはーの確認待ち
**関連**: [addon_catalog_management.md](../intent/addon_catalog_management.md) の「導入時の質問と、アドオン専用の Python 環境」、[addon_setup_scripts_bypass_lock_constraints.md](addon_setup_scripts_bypass_lock_constraints.md)、[stackchan_firmware_not_distributed.md](stackchan_firmware_not_distributed.md)

## 何をする件か

声のアドオン (saiverse-voice-tts) を、公開のアドオンカタログから入れられるようにする。いまの入れ方は「git clone して setup.bat を実行し、参照音声の wav を置く」だけで、Stack-chan Vessel から声を出すにはこのアドオンが必須なのに、カタログには載っていない (2026-10-05 にまはーが気づいた)。

## 決まっていること

- **カタログは、まずまはーのフォーク (`maha0525/saiverse-voice-tts`) を指して載せる。** まはー (2026-10-05):「ひとまず俺のフォークで問題ない」。いずれ Nature109 のリポジトリへ戻す。戻す前に要る本体の修正は、カタログの intent の「カタログが指す先の切り替え」にある。
- **フォークの main には、upstream にまだ入っていない次の 5 つを入れた** (`b964fcd`。2026-10-05、まはーが push を承認した)。upstream の main (`a9be97d`) の上に積んである。
  - GPT-SoVITS の合成の別プロセス化 (upstream の PR #5)
  - 音声のストリームが止まったときに、SAIVerse の終了処理が止まったままにならないよう、待ち時間に上限を付ける修正 (upstream の PR #6)
  - GPT-SoVITS でも、GPU と CPU を設定で選べる変更
  - 音声が作られなかった吹き出しに「音声なし」と表示する変更 (`unavailable_keys`。本体側の表示の処理は main に入っている)
  - 表示名と説明を、日本語と英語で持つ変更

## 載せ方 (メティスの案、まはーの確認待ち)

カタログの intent の「導入時の質問と、アドオン専用の Python 環境」の仕組みを使う。導入時に「使う音声エンジンを選んでください」と訊き、選択肢は次の三つで、複数選べる。

- OpenAI TTS / ElevenLabs (GPU は要らない。API キーを入れるだけ)
- GPT-SoVITS (NVIDIA の GPU が要る。数 GB のダウンロード。Windows と Linux だけに出す)
- Irodori-TTS (NVIDIA の GPU が要る。数 GB のダウンロード。Windows と Linux だけに出す)

GPT-SoVITS と Irodori-TTS は、それぞれ専用の Python 環境に入れる。GPT-SoVITS の requirements は requirements.lock と両立せず、GPT-SoVITS と Irodori-TTS も互いに両立しない (下の「経緯」の 2026-09-11 の洗い出し) ので、本体の venv にも、二つ一緒の環境にも入れられない。

### setup.bat がしている処理の置き換え先

- voice-tts のパッケージ (requirements.txt): `pip_install` の step (本体の venv)。`torchcodec` は Irodori-TTS 用の requirements のファイルへ移す (下の「確かめたこと・確かめ方」)。
- 設定ファイルのひな形 (`config/default.json` と `voice_profiles/registry.json`) の作成: `python_script` の step (本体の Python。パッケージは入れない)。
- GPT-SoVITS 本体と Irodori-TTS 本体の取得: `git_clone` の step (commit で固定)。
- GPT-SoVITS と Irodori-TTS のパッケージと、CUDA に対応した torch: `env` の付いた `pip_install` の step。CUDA に対応した torch のダウンロード先 (`https://download.pytorch.org/whl/cu128` など) は、requirements のファイルに書く。
- GPT-SoVITS と Irodori-TTS のモデルの重みと、NLTK のデータ: `env` の付いた `python_script` の step。
- CUDA が使えるかの確認: 専用の環境には最初から CUDA に対応した torch を入れるので、入れ直しの処理は要らない。使えなかったときの知らせ方は、実装のときに決める。
- Playbook の取り込み (`import_all_playbooks.py --force`): 載せない。本体の DB を書き換える処理で、step の allowlist に載せる種類のものではない。2026-10-05 の時点で、本体の同梱 Playbook に voice-tts 用のノード (`tts_speak`) は無い。

## やる作業

カタログの intent の「この仕組みでやる本体の作業」が済んでいることが前提。そのうえで、voice-tts をカタログに載せるまでに次を全部やる。どれかを残したまま載せると、選択肢の一部が専用の環境を使わずに本体の venv を書き換えるか、動かない。

voice-tts (まはーのフォーク):

1. GPT-SoVITS の合成の別プロセスを、専用の環境の Python で起動する。いまは本体の Python (`sys.executable`) で起動している。
2. **Irodori-TTS の合成を、GPT-SoVITS と同じように別プロセスにし、専用の環境の Python で起動する。** いまは SAIVerse と同じプロセスの中で合成しているので、このままでは Irodori-TTS を選んだ利用者の本体の venv に Irodori-TTS のパッケージが入る。
3. 専用の環境に入れる requirements のファイルを、GPT-SoVITS 用と Irodori-TTS 用に作る (GPT-SoVITS 用は gradio を外す。理由は下の「経緯」の「公開前にやること」の 1)。
4. setup.bat がしている処理を、上の置き換え先のとおり step とスクリプトに置き換え、`addon.json` を manifest v2 にする。
5. 参照音声と合成音声を永続データの規約の場所へ移す (下の「経緯」の「公開前にやること」の 4)。

Stack-chan Vessel:

6. 声のサンプルレートを、エンジンが実際に出した値で送る。v0.5.1 までは常に 32 kHz (GPT-SoVITS の出力) として送っていて、OpenAI TTS と ElevenLabs (24 kHz) の声は Stack-chan で速く高く再生されるはずだった (コードを読んで見つけた。実機の音では未確認)。直した版を v0.5.2 として用意した (2026-10-05、未公開)。

確かめること:

7. 隔離した `SAIVERSE_HOME` と新しい venv に、カタログの導入経路で voice-tts を入れ、選択肢ごとに声が出るところまで確かめる。Windows (まはーの開発機) と Linux (NOVA) の両方で行う。本体の venv のパッケージが、導入の前後で変わっていないことも確かめる (下の「経緯」の「公開前にやること」の 6 の項目も含む)。

## 確かめたこと・確かめ方 (2026-10-05)

- **SAIVerse が対応する Python に、CUDA に対応した torch があるか: ある。** PyTorch の配布ページ (`https://download.pytorch.org/whl/cu128/torch/` と `cu130/torch/`) の一覧で、Python 3.11〜3.14 の Windows 用 (`win_amd64`) と Linux 用 (`manylinux_2_28_x86_64`) が、CUDA 12.8 向けは torch 2.11.0、CUDA 13.0 向けは torch 2.14.1 まで配られていることを見た。どちらを使うかは、NVIDIA のドライバの新しさとの兼ね合いで、作業 3 のときに決める。
- **requirements.txt の `torchcodec` が、本体の venv に torch 系のパッケージを持ち込むか: 持ち込まない。** PyPI の torchcodec 0.17.0 の依存 (`requires_dist`) は、開発用の追加分 (`extra == "dev"`) の numpy・pytest・pillow だけだった。torchcodec は Irodori-TTS だけが使うので、作業 2 で Irodori-TTS を専用の環境へ移すときに、requirements.txt から Irodori-TTS 用の requirements のファイルへ移す。
- **その torch で GPT-SoVITS と Irodori-TTS が実際に声を作れるか: 作業 7 で確かめる。** 一覧を見ても分からないので、実際に合成する。Windows はまはーの開発機、Linux は NOVA (Ubuntu) で、隔離した `SAIVERSE_HOME` と新しい venv にカタログの導入経路で入れ、選択肢ごとに声が出るところまで見る。
- **macOS: GPU で動かすエンジンは選択肢に出さない。** Mac には NVIDIA の GPU が載らないので、CUDA を前提にした GPT-SoVITS と Irodori-TTS の選択肢は、Mac では意味を持たない。確かめる Mac も無い。macOS ではクラウドのエンジンの選択肢だけを出す。そのために、カタログの仕組みに「この選択肢はこの OS でだけ出す」という指定を足す (カタログの intent の「manifest に足すもの」の `platforms`)。

## 経緯

### 2026-09-11 の洗い出し (アドオンカタログの intent の Phase 4-E から、2026-10-05 にそのまま移した)

voice-tts の upstream (元になっているリポジトリ) は `Nature109/saiverse-voice-tts` で、GitHub 上でそれを複製したフォーク `maha0525/saiverse-voice-tts` もある。PR #4〜#6 は maha0525 が作成し、#4 は Nature109 のアカウントがマージした。

この節では、次の語をこの意味で使う。

- **requirements.lock** は、本体が動作を確かめた版に全パッケージを固定した一覧 ([dependency_management.md](../intent/dependency_management.md))。
- **constraints** は、pip の `-c` オプションで渡す「この一覧に書いてある版から動かすな」という指定。
- **venv** は、SAIVerse が使う Python の仮想環境 (virtual environment)。本体とアドオンのパッケージは同じ venv に入る。
- **衝突** は、パッケージ同士の版の条件が両立しないこと (pip check の警告文と同じ呼び方)。
- **GIL** (Global Interpreter Lock) は、Python が一度に一つのスレッドにしか処理をさせない仕組み。

#### 2026-09-11 時点の現状

- 旧手順が前提にしていた PR #4 (音声の配信経路と再生キューの変更) は、2026-05-24 にマージ済み。upstream の main ブランチには、それ以降のコミットが無い。
- `addon.json` に `manifest_version` も `setup` も無い。upstream の main ブランチ、フォークの main ブランチ、まはーの手元の `expansion_data/saiverse-voice-tts/` の三つで確認した。このままでは、アドオンカタログから GPT-SoVITS を入れられない。
- 公開の registry.json に voice-tts は載っていない (載っているのは Elyth・X・stackchan)。
- 合成した音声は、旧来の `~/.saiverse/user_data/voice/out/` に保存されている (`tools/speak/playback_worker.py` の `_OUT_DIR`)。
- ペルソナごとの参照音声は、本体のアップロード処理 (`api/routes/addon.py` の `_resolve_file_dir`) によって `~/.saiverse/user_data/addon_files/saiverse-voice-tts/personas/<persona_id>/` に保存され、その絶対パスが DB の `AddonPersonaConfig.params_json` に記録される。voice-tts の合成では、記録された絶対パスがそのまま使われる (`tools/speak/profiles.py` の `_resolve_ref_audio`)。
- 本体の `saiverse/addon_migrations.py` には、voice-tts の参照音声 (`addon_files/saiverse-voice-tts/` から `addon_data/saiverse-voice-tts/inputs/` へ) と合成音声 (`voice/out/` から `addon_data/saiverse-voice-tts/outputs/` へ) を移す処理がすでにある。`ENABLED_ADDONS_FOR_STARTUP` に voice-tts が入っていないので、起動時には実行されない。
- upstream にまだマージされていない PR が 2 本ある。#5 は GPT-SoVITS の合成の別プロセス化 (2026-05-24 に作成)。#6 は、音声のストリームが止まったときに SAIVerse の終了処理が止まったままにならないよう、待ち時間に上限を付ける修正 (2026-08-27 に作成)。まはーの手元の `feature/tts-out-of-process` ブランチ (#5 のブランチ) には、`tools/speak/engine/gpt_sovits.py` と `tools/speak/playback_worker.py` にコミットされていない変更がある。
- voice-tts には Windows 用の `setup.bat` しかなく、`setup.sh` は無い。voice-tts の requirements.txt のコメントには、どのプラットフォームでも使える導入コマンドとして `python scripts/install_backends.py gpt_sovits` が書かれている。

#### 旧手順の 3 番が、いまのままでは成り立たない理由

旧手順 (この節の最後に残してある) の 3 番は、「`setup.bat` を `platform_script` の step で登録する」としていた。これは requirements.lock の導入 (2026-09-02) より前に書かれたもので、いまは次の二つの理由で成り立たない。3 番を計画から外すかどうかは、まはーが決める。

1. `setup.bat` は `scripts/install_backends.py` を通して、GPT-SoVITS の requirements.txt をそのまま `pip install -r` する。その中の `numpy<2.0` と `pydantic<=2.10.6` は、requirements.lock (numpy は Python 3.12 以上で 2.5.2、3.11 で 2.4.6。pydantic は 2.13.5) と両立しない。さらに、`gradio<5` で入る gradio 4.44.1 は `pillow<11` を、それが連れてくる gradio-client 1.3.0 は `websockets<13` を、`torchmetrics<=1.5` で入る torchmetrics 1.5.0 は `numpy<2.0` を要求していて、requirements.lock の pillow 11.3.0・websockets 16.1.1・numpy と衝突する。
2. `saiverse/addon_installer.py` では、requirements.lock が constraints として pip に渡されるのは `pip_install` の step だけで、`platform_script` の step で実行されるスクリプトには渡されない ([addon_setup_scripts_bypass_lock_constraints.md](addon_setup_scripts_bypass_lock_constraints.md))。旧手順のまま実行すると、venv の本体のパッケージが requirements.lock の版から引き下げられる。

#### 公開前にやること (2026-09-11 にメティスが洗い出した。進め方はまはー未決)

1. **voice-tts 用の GPT-SoVITS の requirements を、voice-tts 側で持つ。** GPT-SoVITS の requirements.txt を元に、次を変える。
   - `gradio` を外す。GPT-SoVITS の推論で読み込まれるコード (`GPT_SoVITS/TTS_infer_pack/TTS.py` から import を辿れる範囲) は、gradio を import していない。gradio を import しているのは、WebUI の 5 つ (`webui.py`、`GPT_SoVITS/inference_webui.py`、`GPT_SoVITS/inference_webui_fast.py`、`tools/uvr5/webui.py`、`tools/subfix_webui.py`) と `tools/my_utils.py` だった。`tools/my_utils.py` を import しているのは、WebUI、学習・データ準備・書き出し用のスクリプト、実験的なストリーミング推論のスクリプト (`GPT_SoVITS/stream_v2pro.py`) で、voice-tts の入口 (`tools/speak/engine/gpt_sovits.py` の `from TTS_infer_pack.TTS import TTS, TTS_Config`) から辿れる範囲には無い。これはコードを辿った確認で、gradio を外した venv で合成してはいない。
   - `numpy<2.0` と `pydantic<=2.10.6` の上限を外す。2026-09-03 00:11 の再起動で、numpy 2.5.2 と pydantic 2.13.5 が入った venv (まはーの開発機、Windows、Python 3.13) のまま、voice-tts の合成は成功している ([dependency_management.md](../intent/dependency_management.md) §5 の 6)。
   - `torchmetrics<=1.5` を `torchmetrics>=1.5.2` にする。torchmetrics は `GPT_SoVITS/AR/models/t2s_model.py` が import していて、推論で必要になる。1.5.0 は `numpy<2.0` を要求するが、1.5.2 以降は numpy の上限を持たない (PyPI で確認)。1.5.2 以降で推論が通るかは未確認。
   - voice-tts の requirements.txt に、numba の版の条件を足す ([dependency_management.md](../intent/dependency_management.md) §3-3 に残っている宿題)。まはーの開発機の venv では 2026-09-02 20:47 に numba 0.67.0 へ上がっていて、9/3 の合成はその版で成功した。
2. **`saiverse/addon_installer.py` で、`platform_script` / `python_script` の step で実行されるスクリプトの中の pip にも、requirements.lock が constraints として渡るようにする (本体側)。** [addon_setup_scripts_bypass_lock_constraints.md](addon_setup_scripts_bypass_lock_constraints.md)。
3. **`addon.json` を manifest v2 にする。** `manifest_version`・`setup_version`・`data_subdirs`・`setup.steps` を書く。1 の requirements をどの step で入れるか (`pip_install` の step に分けるか、スクリプトの中に残すか) は未決。スクリプトを使うなら、`platform_script` の step の `unix` 側のスクリプトも要る。いまの `saiverse/addon_installer.py` は、実行中の OS 向けのスクリプトが無いとその step を失敗にせず飛ばして先へ進むので、`setup.sh` が無いまま載せると、macOS と Linux では GPT-SoVITS が入らないまま導入が成功したように見える。
4. **参照音声と合成音声を、永続データの規約の場所へ移す。** 本体の `ENABLED_ADDONS_FOR_STARTUP` に voice-tts を加えると、起動時に参照音声のファイルが `addon_data/saiverse-voice-tts/inputs/` へ移る。しかし DB に記録された参照音声の絶対パスと、本体のアップロード処理の保存先は、古い `addon_files/` のまま残る。合成では記録された絶対パスがそのまま使われるので、参照音声のファイルが見つからずにエラーになる。移行を有効にするときは、同じリリースで次の三つを揃える (2026-05-23 のインシデントの教訓「コード path 変更と migration はセット commit」)。
   - 本体: voice-tts の参照音声のアップロード先を、移行先の `addon_data/saiverse-voice-tts/inputs/` と揃える。ファイルを受け付ける他のアドオンの保存先をどう扱うかも、あわせて決める。
   - 本体: DB の `AddonPersonaConfig.params_json` に記録された、古い場所の絶対パスを書き換える。
   - voice-tts: 合成音声の保存先 (`_OUT_DIR`) を `get_addon_data_dir(...)/outputs/` に変える。
5. **upstream に PR を出してマージし、registry.json に voice-tts を載せる。**
6. **公開前の検証。** 隔離した `SAIVERSE_HOME` と、requirements.lock だけを入れた新しい venv に、アドオンカタログの導入経路で voice-tts を入れ、GPT-SoVITS で実際に声が出るところまで確かめる。[dependency_management.md](../intent/dependency_management.md) §5 の 4 で「voice-tts の実導入は本番 venv の同期のときに」と後に回していた検証にあたる。証拠は次の項目ごとに残す。2026-09-11 の時点では、すべて未検証。
   - Windows で、カタログから入れて声が出る: 未検証。
   - macOS で、カタログから入れて声が出る: 未検証 (`setup.sh` が無い)。
   - Linux で、カタログから入れて声が出る: 未検証 (`setup.sh` が無い)。
   - 参照音声を設定済みのペルソナがいる既存の環境に 4 の移行を当てて、そのペルソナの声が出る: 未検証。

#### まはーが決めること (2026-09-11 時点で未決)

- **旧手順の 3 番 (`setup.bat` を `platform_script` の step で実行する) を、計画から外すか。**
- **upstream にまだマージされていない PR #5・#6 を、公開前にマージするか。** #5 は GPT-SoVITS の合成を別プロセスに移して、SAIVerse 本体のどのスレッドが GIL を握り続けても合成が遅くならないようにするもので、[mcp_cancel_scope_spin_gil_starvation.md](mcp_cancel_scope_spin_gil_starvation.md) の修正方針 B にあたる。GIL 飢餓の元になった本体側の不具合を直す修正方針 A は、2026-05-24 に回帰テストと一緒に実装済み。
- **Irodori-TTS を公開に含めるか。** Irodori-TTS の pyproject.toml は `transformers>=5.12.1,<6`・`peft>=0.18.0`・`gradio>=5.0.0` を要求していて、GPT-SoVITS の requirements.txt の `transformers>=4.43,<=4.50`・`peft<0.18.0`・`gradio<5` と両立しない。同じ venv に両方は入らない。`addon.json` のエンジンの選択肢 (`engine`) には、今も `irodori` がある。

#### 旧手順 (2026-05-23。3 番は、上に書いた理由でいまのままでは成り立たない)

voice-tts repo は `origin = Nature109/saiverse-voice-tts` の共有 repo で、 現在 PR #4 (subscribe-before-open) がレビュー待ち。 v2 化は別 PR で出す方針:

1. まはー: ナチュレに「manifest v2 化 PR を別途出す」連絡
2. main 起点で `feature/manifest-v2` を切る
3. addon.json v2 化 (manifest_version=2, setup_version, data_subdirs)、 `setup.bat` を `platform_script` step で登録 (skip_if_exists=`external/GPT-SoVITS`)、 永続データ参照を `get_addon_data_dir(...)/inputs/` と `get_addon_data_dir(...)/outputs/` に変更
4. PR 提出 → ナチュレレビュー → マージ
5. マージ後、 SAIVerse 本体の `ENABLED_ADDONS_FOR_STARTUP` に `saiverse-voice-tts` を追加、 registry.json に voice-tts エントリを追加
