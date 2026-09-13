# 更新ログの pip check の警告が、アドオンの入れ直しでは解けない衝突にも「アドオンを入れ直す」と助言する

**起票**: 2026-09-11 (まはーの環境で v0.3.12 へ更新したときに出た警告の調査から)
**状態**: 未着手
**優先度**: low
**関連**: `scripts/update_engine.py` の `_report_addon_conflicts`、[dependency_management.md](../intent/dependency_management.md) §3-3、[addon_setup_scripts_bypass_lock_constraints.md](addon_setup_scripts_bypass_lock_constraints.md)、[addon_catalog_management.md](../intent/addon_catalog_management.md) Phase 4-E

## 出た警告

2026-09-11 21:57 に、まはーの環境で v0.3.12 へ更新したとき、更新ログに次の警告が出た。pip check は、venv に入っているパッケージ同士で版の条件が両立しないもの (dependency_management.md と警告文では「衝突」と呼んでいる) を調べる pip のコマンドで、更新の最後に実行している。

```
[deps] pip check: gradio 4.44.1 has requirement pillow<11.0,>=8.0, but you have pillow 11.3.0.
[deps] pip check: gradio-client 1.3.0 has requirement websockets<13.0,>=10.0, but you have websockets 16.1.1.
[deps] pip check: torchmetrics 1.5.0 has requirement numpy<2.0,>1.20.0, but you have numpy 2.5.2.
[deps] 上の 3 件は requirements.lock の外にあるパッケージ (アドオンか手で入れたもの) との衝突です。本体の更新自体は完了しています。該当のアドオンは入れ直す (アドオンを入れ直す) 必要があるかもしれません。
```

## 調べて分かったこと (2026-09-11)

- gradio・gradio-client・torchmetrics は、voice-tts が GPT-SoVITS の requirements.txt (`gradio<5`、`torchmetrics<=1.5`、`pytorch-lightning>=2.4` を含む) を入れたときに入ったもの。venv の dist-info の日付は、3 つとも 2026-04-17 13:56 だった。
- 本体の requirements.txt に gradio があったのは 2026-01-30 (コミット 267a4e4f で削除) までで、版は 5.38.0 だった。最初の公開版 v0.1.0 (2026-02-11) の requirements.txt には gradio が無いので、公開版から入れたユーザーの venv に本体が gradio を入れたことは無い。
- 衝突の相手の pillow 11.3.0・websockets 16.1.1・numpy 2.5.2 は 2026-09-01 04:18 から venv に入っていて、requirements.lock のこの 3 つの版は 2026-09-02 の導入から変わっていない。v0.3.12 への更新で新しく生まれた衝突ではない。
- 2026-09-03 00:11 の再起動で、同じ組み合わせのまま voice-tts の合成は成功していた ([dependency_management.md](../intent/dependency_management.md) §5 の 6)。
- voice-tts は公開の registry.json に載っていない。この 3 件が出るのは、voice-tts の `setup.bat` か、`python scripts/install_backends.py gpt_sovits` (voice-tts の requirements.txt が、プラットフォームを問わない入口として案内している) を手で実行して、GPT-SoVITS を入れた環境になる。

## 問題

警告の最後の文は「該当のアドオンは入れ直す (アドオンを入れ直す) 必要があるかもしれません。」と助言する。しかしこの 3 件は、アドオンを入れ直しても消えない。GPT-SoVITS の requirements.txt 自体が `numpy<2.0` を要求していて、requirements.lock の numpy (Python 3.12 以上で 2.5.2、3.11 で 2.4.6) と両立する組み合わせが無いからだ。そのうえ voice-tts の `scripts/install_backends.py` は constraints を渡さずに pip を呼ぶので、助言どおりに入れ直すと、venv の numpy などを requirements.lock の版から引き下げに行く ([addon_setup_scripts_bypass_lock_constraints.md](addon_setup_scripts_bypass_lock_constraints.md))。

文面には「入れ直す (アドオンを入れ直す)」という、同じ語の重複もある。

## 直し方の方向 (未決)

アドオンの入れ直しでは解けない衝突 (アドオンが入れる外部のリポジトリの requirements が requirements.lock と両立しない場合) があることを前提に、助言の文面を見直す。

voice-tts については、[addon_catalog_management.md](../intent/addon_catalog_management.md) Phase 4-E の「voice-tts 用の GPT-SoVITS の requirements を voice-tts 側で持つ」作業が済めば、新しく入れる環境ではこの 3 件は出なくなる。すでに gradio 4.44.1 が入っている venv では、gradio と gradio-client を手で削除するまで、gradio の 2 件は残る。
