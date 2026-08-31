# ペルソナ ID からパスを組む箇所が散在し、境界の保証が使用側に無い

**ステータス: 未着手** (2026-08-09 起票。Wave 0 の Building ID 工事のレビュー第 7 巡で浮上)

## 症状

`persona_id` は `~/.saiverse/personas/<id>/` のフォルダ名としてそのまま使われる。
この連結は**少なくとも 10 箇所**に散っている:

- `saiverse/data_paths.py` (`get_persona_memory_db`)
- `saiverse/day_report.py` (3 箇所)
- `saiverse/uri_resolver.py`
- `sai_memory/backup.py` (2 箇所)
- `saiverse_memory/adapter.py`
- `persona/core.py` (2 箇所 — `log.json` / `conscious_log.json`)

どこにも「組み立てた結果が親ディレクトリの内側か」の検査が無い。区切り文字や
`..` を含む ID は SAIVERSE_HOME の外へ出る。

## なぜ作成時の検査では足りないか

2026-08-09 に `_create_persona` と `spawn_entity_from_blueprint` へ
`is_safe_path_component` を入れた (2026-08-16 にさらに強い文字種契約
`is_valid_identifier` へ載せ替え — building_id_no_charset_constraint 論点 3)。
**これは新しく作る ID を狭めただけで、境界を保証していない。** 通らない経路が残る:

- 既に DB にある ID (この検査より前に作られたもの)
- import / エクスポート復元 / DB の直接編集で入った値
- 上記 10 箇所は ID の出所を問わずパスを組む

**歯止めは「値が生まれる場所」ではなく「値が効く場所」に要る。** 作成の口だけを
塞ぐと、塞いだ側が「守られている」と誤解する分だけ危うい (この issue はその誤解を
記録するために起票した)。

## 直すときの論点

1. **中央の安全なパス結合**: `safe_child_path(parent, component)` を一つ作り、
   `resolve()` した結果が `parent` の内側であることを検査して返す。上記 10 箇所を
   これに寄せる。ID を検査するのではなく**結果のパスを検査する**のが要点
   (ID の文字種契約とは独立に成り立つ)。
2. **既存データの扱い**: 起動時に危険な ID を検出して隔離するか、警告して止めるか、
   放置か。Building ID の裁定 (既存は放置) と揃えるかは別途判断が要る。
3. **同族の点検**: City 名・Building ID もパスへ入る
   (`~/.saiverse/cities/<city>/buildings/<id>/log.json`)。City 名は ASCII 検証済み、
   Building ID は 2026-08-09 に作成の口を塞いだが、**どちらも使用側の検査は無い**。
4. **脅威の格**: 単一ユーザーのローカルアプリなので、悪意ある攻撃者より「事故で
   区切り文字が入った ID」「移行スクリプトが作った値」の方が現実的。優先度は
   その前提で決める。

## 関連

- `docs/issues/building_id_no_charset_constraint.md` — 作成の口を塞いだ工事 (論点 3 が AIID)
- `docs/issues/entity_creation_has_no_transactional_boundary.md` — 同じ工事から出たもう一つの構造課題
