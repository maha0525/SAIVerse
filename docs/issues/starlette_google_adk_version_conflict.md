# Issue: starlette バージョン競合 (google-adk が要求する範囲と SAIVerse が固定する範囲が衝突)

**ステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-20
**関連**: `requirements.txt`, インストール済み `google-adk 1.5.0`

## 背景

`google-genai` を 1.56.0 から 1.75.0 にアップグレードした際 (2026-05-20、`docs/intent/thought_signature_persistence.md` 関連)、`pip` の dependency resolver から以下の警告が出た:

```
ERROR: pip's dependency resolver does not currently take into account all the packages that are installed.
This behaviour is the source of the following dependency conflicts.
google-adk 1.5.0 requires starlette>=0.46.2, but you have starlette 0.41.3 which is incompatible.
```

`pip show google-adk` の結果:

```
Name: google-adk
Version: 1.5.0
Required-by:
```

つまり **`google-adk` は SAIVerse 内のどのパッケージからも依存されていない** (Required-by が空)。`requirements.txt` にも記載されていない。過去に手動 install されたか、別作業の過程で副次的に install されたものと推測される。

starlette 0.41.3 は SAIVerse が pin している `fastapi==0.116.1` 経由でインストールされている (fastapi 0.116.1 の依存範囲)。

## 何が問題か

- **現時点で動作影響はない**: google-adk は SAIVerse のどこからも import されていないため、starlette のバージョン違いで挙動が壊れる箇所がない
- ただし `pip install` のたびに警告が出続けるのは、本物の dependency conflict を見落とすノイズになりうる
- google-adk が将来何かの依存ツリーに紛れ込んだとき (例: 別の Google ライブラリが indirectly に要求する場合) に潜在的な不整合になる

## 解決案候補

### 案 A: google-adk を uninstall する (推奨)

Required-by が空で SAIVerse から使われていないので、削除して問題ない。

```bash
python -m pip uninstall google-adk
```

ただし「過去に何かのテスト用途で入れたかも」を確認するため、grep で参照箇所を念のため確認してから実行する:

```bash
grep -rn "google.adk\|google_adk" --include="*.py" .
```

### 案 B: fastapi / starlette を上げて google-adk の要求を満たす

fastapi 0.116.1 → 最新版 (starlette 0.46.2+ を含むバージョン) に上げる。本タスクのスコープ外なので慎重に検証する必要あり。

### 案 C: 放置する

動作影響がないので警告を無視する。ただし他の本物 conflict を見落とすリスクあり。

## 関連リソース

- `docs/intent/thought_signature_persistence.md` — google-genai 1.75.0 更新で本件発覚
- `requirements.txt` — fastapi / google-genai のバージョン pin
- pip dependency conflict は本来 SAIVerse の依存ツリーに無関係 (google-adk は外部要因)

## ログ

- 2026-05-20: issue 起票。`google-genai` を 1.56.0 → 1.75.0 にアップグレードした際の pip 警告から発覚。google-adk は Required-by が空で SAIVerse 本体からは未使用。推奨は案 A (uninstall) だが、別タスクで実施する。
