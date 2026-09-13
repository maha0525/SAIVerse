# アドオンの setup で platform_script / python_script の step が実行するスクリプトの中の pip には、requirements.lock が constraints として渡らない

**起票**: 2026-09-11 (voice-tts をアドオンカタログに載せる前に必要な作業を洗い出す中で見つけた)
**状態**: 未着手
**優先度**: high (メティスの判断。voice-tts を今の setup のままアドオンカタログに載せると、setup の step を持つアドオンとして最初にこの経路を通るため)
**関連**: [dependency_management.md](../intent/dependency_management.md) §2-2・§2-3・§2-4、[addon_catalog_management.md](../intent/addon_catalog_management.md) Phase 4-E、[pip_check_warning_reinstall_advice.md](pip_check_warning_reinstall_advice.md)、`saiverse/addon_installer.py`

## 現象

`saiverse/addon_installer.py` は、アドオンカタログからアドオンを入れるときに、addon.json (manifest v2) の setup の step を順に実行する。[dependency_management.md](../intent/dependency_management.md) §2-4 は「アドオンは本体の部品を動かせない」を不変条件にしていて、その持ち主を「`addon_installer.py` が constraints を渡す」としている。constraints は、pip の `-c` オプションで渡す「この一覧に書いてある版から動かすな」という指定のこと。

実際に requirements.lock が constraints として pip に渡されるのは、setup の step のうち `pip_install` だけだった (2026-09-11 にコードで確認)。

- `pip_install` の step (`_exec_pip_install`) では、`python -m pip install -r <アドオンの requirements> -c requirements.lock` が実行される。
- `platform_script` の step (`_exec_platform_script`) と `python_script` の step (`_exec_python_script`) では、アドオンのスクリプトが、SAIVerse のプロセスの環境変数をそのまま写した環境で起動される。requirements.lock を constraints として渡す指定は付かない。

そのため、スクリプトの中で pip を呼ぶアドオンは、venv (SAIVerse が使う Python の環境) に入っている本体のパッケージを、requirements.lock の版から動かせる。

## いま影響を受けるもの

公開の registry.json に載っている Elyth・X・stackchan は、どれも setup の step を持たないので、2026-09-11 の時点ではこの経路を通るアドオンは無い。

voice-tts を今の setup のままアドオンカタログに載せると、最初にこの経路を通る。[addon_catalog_management.md](../intent/addon_catalog_management.md) の Phase 4-E の旧手順 (2026-05-23) は、voice-tts の `setup.bat` を `platform_script` の step で実行する予定にしていた。`setup.bat` は voice-tts の `scripts/install_backends.py` を呼び、その中で GPT-SoVITS の requirements.txt が constraints なしで `pip install -r` される。GPT-SoVITS の requirements.txt は `numpy<2.0` と `pydantic<=2.10.6` を要求していて、requirements.lock (numpy は Python 3.12 以上で 2.5.2、3.11 で 2.4.6。pydantic は 2.13.5) と両立しない。constraints が無いまま実行すると、pip は venv の numpy と pydantic を requirements.lock の版から引き下げに行く。2026-09-01 に本体の pip install が voice-tts の numba を壊した事故 ([dependency_management.md](../intent/dependency_management.md) §1-1) の、向きが逆のものになる。

## 直し方の方向 (未決)

pip は環境変数 `PIP_CONSTRAINT` でも constraints を受け取る。`addon_installer.py` が `platform_script` と `python_script` のスクリプトを起動するときに、`PIP_CONSTRAINT` に requirements.lock のパスを入れて渡せば、スクリプトの中の pip にも requirements.lock の版が効く。requirements.lock と両立しない要求を持つアドオンは、本体のパッケージを黙って動かす代わりに、導入の時点で pip が失敗して理由が出るようになる。

直すときに確かめること:

- `PIP_CONSTRAINT` が `-c` と同じ範囲に効くか。環境変数は、pip がソースからパッケージをビルドするときに裏で起動する pip にも引き継がれることがあり、ビルドに使うパッケージにまで版の指定がかかって、`-c` と振る舞いが違う可能性がある (未確認。pip の変更履歴で確かめる)。voice-tts の `setup.bat` と `scripts/install_backends.py` のコメントには、editdistance や opencc をソースからビルドすることがあると書いてある。
- voice-tts の `setup.bat` が行う CUDA 版 torch の入れ直し (`--index-url https://download.pytorch.org/whl/cu128 --force-reinstall`) が、`PIP_CONSTRAINT` の下でも通るか。torch 自体は requirements.lock に載っていないが、`--force-reinstall` は torch の依存も入れ直す。その中の filelock・fsspec・sympy・typing-extensions は requirements.lock に載っているので、PyTorch の配布元にその版が無ければ、解決に失敗する可能性がある。
- `pip_install` の step と同じく、`platform_script` / `python_script` の step でも constraints が渡ることをテストで固定する。
- 直したら、[dependency_management.md](../intent/dependency_management.md) のステータス行と §2-2・§2-3・§2-4 に付けた注記を外す。
