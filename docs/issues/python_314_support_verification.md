# Issue: Python 3.14 非対応の制約が解消済み — 実機検証してドキュメントの上限を外す

**ステータス**: 🔲 未着手（実機検証待ち）
**優先度**: low
**作成日**: 2026-09-01
**関連**: `requirements.txt` (`fastembed`), `sai_memory/memory/recall.py`, `pyproject.toml`

## 背景

ドキュメント 4 箇所が「Python 3.11〜3.13 で動作、3.14 以降は非対応」と書いている。この上限を宣言しているコードは無く（`pyproject.toml` は `requires-python = ">=3.11"` で下限のみ、`setup.bat` / `setup.sh` もバージョンの上限チェックをしていない）、出自が長らく不明だった。

2026-09-01 に出自を特定した。**onnxruntime である。**

- 2026-02-13、Discord コミュニティで、あるユーザーが `setup.bat` 実行時に `ImportError: DLL load failed while importing onnxruntime_pybind11_state` を踏んだ。当時の回答で「Python 3.10〜3.13 までしか正規に対応されていないようだ」と案内され、それがそのままドキュメントの上限になった（この記述はサイトのたたき台コミット `663c69c`、2026-07-06 に既に入っている）。
- SAIVerse は onnxruntime を直接は要求していない。`requirements.txt` の `fastembed>=0.7.3` が引いており、`sai_memory/memory/recall.py` の想起経路が `import onnxruntime` を通る。壊れると SAIMemory の読み書き（深い想起）ができなくなる。

## 現況（2026-09-01 時点の調査）

**鎖のどこにも 3.14 を止める石が残っていない。**

| 対象 | 確認した内容 |
|---|---|
| onnxruntime 1.29.0 (2026-08-17 リリース) | classifier に `Python :: 3.14` あり。`Requires: Python >=3.11` |
| fastembed 0.7.4 | `Requires-Python: >=3.9.0`（上限なし） |
| fastembed → onnxruntime の制約 | Python 3.13 以上で `onnxruntime >1.20.0`（上限なし）＝ 1.29.0 が解決されうる |
| fastembed の依存分岐 | `numpy (>=2.3.0) ; python_version >= "3.14"` という行があり、3.14 で使われる前提で書かれている |
| SAIVerse `pyproject.toml` | `requires-python = ">=3.11"`（上限なし） |
| `setup.bat` / `setup.sh` | Python バージョンの上限チェックなし |

なお開発機に入っている onnxruntime が 1.24.1 なのは、インストール時期が古いだけ。fastembed が上限を掛けていないので、新規インストールでは 1.29.0 が入る。

## 残っている作業

**実機検証のみ。** 条件は揃ったが、**Python 3.14 で SAIVerse を起動した人はまだいない**（2026-09-01 まはー裁定: 動くのを確認してからドキュメントを変える）。

1. Python 3.14 を入れた環境で `setup.bat`（または `setup.sh`）を通す
2. 埋め込みモデルのダウンロードが完走するか
3. 起動して、SAIMemory の意味検索（深い想起）が動くか — ここが onnxruntime を実際に踏む経路

## 検証が通ったら直す箇所

4 箇所とも同じ文言なので、揃えて直す。

- `README.md:134`
- `docs/developer-guide/contributing.md:28`
- `docs/getting-started/installation.md:7`
- 公式サイト（別リポジトリ `SAIVerse-docs`）の `docs/guide/getting-started.md`

## 関連リソース

- ユーザーが踏んだ当時のエラー: `ImportError: DLL load failed while importing onnxruntime_pybind11_state`
- 同じエラーは Visual C++ 再頒布可能パッケージ (x64) の欠如でも起きる。Python バージョンだけが原因とは限らない点に注意
