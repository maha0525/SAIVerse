# アドオンの更新で step が途中で失敗すると、次の更新で setup が再実行されない

**起票**: 2026-10-05 (導入時の質問と専用の Python 環境の実装の検収で言葉になった。穴そのものは旧実装からある)
**状態**: 未着手
**優先度**: medium (メティスの判断。最初に踏みうるのは voice-tts の更新 — 数 GB のダウンロードを含む setup が途中で失敗する確率は小さくない)
**関連**: [addon_catalog_management.md](../intent/addon_catalog_management.md)、`saiverse/addon_installer.py` の `execute_update_plan`

## 現象

アドオンの更新は「新しい commit へ `git checkout` → setup_version が上がっていれば setup の step を実行」の順で進む。step が途中で失敗すると、アドオンのフォルダは新しい commit のまま残る。新しい commit の addon.json は新しい setup_version を持っているので、次に更新を試みても「setup_version 変更なし」と判定され、失敗した setup は二度と実行されない。

利用者から見える形: 更新が途中で失敗した (例: 数 GB のダウンロード中に回線が切れた) あと、もう一度「更新」を押しても、今度は何も実行されずに成功したように見える。実際には setup の後半 (入らなかったパッケージ、落ちなかったファイル) が欠けたまま動く。

導入 (install) にはこの穴は無い — 失敗したらアドオンのフォルダごと消すので、やり直しは最初からになる。

## 直し方の候補 (未決)

- 「setup がどこまで済んだか」を導入物の記録 (`~/.saiverse/addon_install/<id>/`) に持ち、成功して初めて「この setup_version は済み」と記す。再実行の判定を addon.json の setup_version 同士の比較ではなく、この記録との比較にする。
- もっと軽い形: 更新の step が失敗したら、その旨を記録に残し、次の prepare が「前回の setup は途中で失敗している」と知らせて setup をやり直す。

どちらも、済んだ step を二度実行することになる (step は `skip_if_exists` などで再実行に耐える前提)。
