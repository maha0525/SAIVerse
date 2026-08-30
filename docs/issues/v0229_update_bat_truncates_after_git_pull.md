# v0.2.29 の update.bat は git pull の直後に静かに死ぬ — 依存更新が一切走らない

**発見**: 2026-08-30 (段 3: update.bat 経路の実機検証、隔離複製リポジトリ)
**状態**: 🔲 未解決 — コードでは直せない (欠陥はリリース済みの v0.2.29 側 update.bat にある)。対処は **v0.3.0 リリースノートに「v0.2.x からの更新は update.bat を 2 回実行する」を明記すること**。この一文が載るまでこの issue は閉じない。
**深刻度**: P1 相当 — 全 v0.2.29 ユーザーのアップグレード初回が「コードだけ新しく、依存が古い」中途半端な状態で止まる。ただし 2 回目の実行で完全に自己回復する (実機確認済み)。

## 実測 (2026-08-30、v0.2.29 複製 + 隔離プロファイルで実走)

v0.2.29 の update.bat を実行すると:

1. `git pull` は成功し、コードは新リリースへ fast-forward される。
2. その直後、実在しない stash の案内 (「Your local changes are saved in git stash. Run 'git stash pop'」) を出して **exit 0 で終了**する。
3. pip install / database migrate / playbook import / npm install は**一切実行されない**。

## 原因

cmd のブロック解析の欠陥。v0.2.29 の update.bat には
`echo [OK] Code updated (local changes stashed)` という行があり、echo 引数内の
括弧が `if !errorlevel! neq 0 ( ... ) else ( ... )` のブロック構造を壊す。
最小再現 (`parse_test.bat`) で、errorlevel=0 でも stash ブロック内の echo が
外へ漏れて実行され、以降のフローが崩壊することを確認した。
pull 成否に関係なく、この bat は毎回コード更新だけで打ち切られる。

## ユーザーへの実害と回復手順

- 1 回目の update.bat 後に start.bat を叩くと、v0.3 で増えた依存
  (mcp / feedparser / cryptography / curl-cffi / imageio-ffmpeg など) が venv に
  無く、backend が ModuleNotFoundError で落ちる。
- **update.bat をもう一度実行すれば完全回復する**: 1 回目の pull でコードは
  v0.3.0 になっているため、2 回目は新しい update.bat → `scripts/update_engine.py
  --manual` が走り、スナップショット退避 → pip → npm ci まで正しく完了する
  (隔離複製で実機確認済み)。
- ただしこの回復は、v0.3.0 に次の 2 修正が入っていることが前提:
  - `dc9650cb` — snapshot.py の sys.path shim (無いと 2 回目がスナップショット段で
    ModuleNotFoundError 中断)
  - `302680e6` — requirements.txt の ASCII 化 (無いと日本語 Windows で pip 段が
    UnicodeDecodeError 中断)

## やること (リリース工程)

- [ ] v0.3.0 リリースノートに明記: 「v0.2.x からの更新は **update.bat を 2 回**
  実行してください。1 回目はコードの取得だけで止まります (画面に git stash の
  案内が出ますが、stash は作られていません — 無視してください)。2 回目で
  依存関係の更新まで完了します。」

## 関連

- [`2026-08-29_v0229_upgrade_test.md`](../handoff/2026-08-29_v0229_upgrade_test.md) — 段 3 の検証記録
- [`scripts/update_engine.py`](../../scripts/update_engine.py) — 現行の fail-closed 更新エンジン (2 回目に走る側)
