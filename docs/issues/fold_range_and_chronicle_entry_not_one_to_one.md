# 退場する範囲と、それを覆うあらすじの範囲が一対一であることを誰も強制していない

**ステータス**: 未解決 (2026-07-27 発見。`chronicle_run_boundary_lost_by_excluded_tag` の修正に対する Codex 攻撃レビュー 四巡目で発掘)
**深刻度**: P2 — 現時点で発生させる経路は確認できていない (下の「到達性」参照)。ただし破れると提示層の時系列が壊れる
**関連**: `sea/eviction_plan.py` の `compile_groups_from_folds` ・ `sea/session_lifecycle.py` の `_attach_chronicle_refs` / `_apply_eviction_plan` ・ [`../intent/experience_structure.md`](../intent/experience_structure.md) §4-5 ・ [`../intent/chronicle_eviction.md`](../intent/chronicle_eviction.md)

---

## 共通の根

退場の設計は「**畳んで消える範囲 = そのあらすじが覆う範囲**」で立っている。提示層に置かれる圧縮区間の置き換えは、その範囲の生ログの代わりに読まれるものだから、範囲がずれれば「そこに無かったものが有ったことにされる / 有ったものが二重に出る」。

ところがこの一対一を**強制している場所が無い**。`EvictionPlan.folds` の docstring は「時系列順・互いに重ならない」と書いているが検算は無く、あらすじ側の範囲との照合も無い。現に二つの形で破れうる。

## 症状 1: 検算した分割が退場適用へ伝播しない

`compile_groups_from_folds` (2026-07-27 追加) は、fold が「今回退場しないメッセージ」をまたいでいたら編纂へ渡す範囲を割る。しかしこれは**編纂側にだけ効く** — `_apply_eviction_plan` に渡るのは元の `plan.folds` のままなので、提示層の圧縮区間は割れていない範囲を覆い続ける。

Fold=`[m0, m2]` で `m1` が退場しない場合:

- 編纂: あらすじは `[m0]` と `[m2]` に分かれる (偽の隣接なし)
- 提示: 圧縮区間は `[m0, m2]` を 1 つの置き換えとして m0 の位置に置き、その後ろに生ログの m1 が並ぶ → 提示の並びが体験の並びと食い違う

**記憶 (永続) の真実を優先して編纂側だけ先に直した**のが現状。提示層は一時的な表示であり、両方直すには退場適用 (`_apply_eviction_plan` / `_record_partial_episode` の子 episode 刻み) の意味論に手を入れる必要があるため分離した。

**あるべき形**: 検算の結果を「修復済みの `EvictionPlan`」として返し、編纂と退場適用の**両方が同じ計画を見る** (single source of truth)。分割された fold の `open_episode_ref` を両片が持つときの子 episode 刻みの扱いが要検討 (2 回刻んでよいのか、片方に寄せるのか)。

## 症状 2: `_attach_chronicle_refs` が「包含」でなく「重なり」で entry を付ける

`sea/session_lifecycle.py:1183` 付近。fold の message id と **1 件でも重なる** entry を、その fold の `chronicle_entry_ids` に付ける。entry の `source_ids` がその fold に**収まっている**ことは確認していない。

全量整理 (`compile_groups=None` の経路 — force / session close / organize-memory / anchor 失効) は退場範囲と無関係に一次あらすじを作るので、後から退場する複数の範囲と、その間に残るメッセージまで一つの entry が覆いうる。その entry が fold に付くと:

- 圧縮区間の置き換えが、退場せず生ログのまま残るメッセージまで語り直す (二重)
- 同じ広域 entry が複数の fold に付けば、同じ内容が離れた位置に二度出る

**確信度**: 機構 (重なりで付く / 包含チェックが無い) はコードで確認した事実。実際にユーザー環境で起きたかは未確認。

## 到達性 (なぜ P1 でなく P2 か)

症状 1 は「fold が連続でない」ときにだけ現れる。現行の `plan_eviction` は連続した run からしか Fold を作らないので、**今日の経路では発生しない** (`tests/test_eviction_plan.py::CompileGroupsFromFoldsTest::test_plan_eviction_output_is_never_split` が往復で固定)。症状 2 は全量整理と退場が混在する実データで起こりうるが、実測はしていない。

## 直す方向 (未検討)

1. 検算を「修復済み計画」を返す形にし、編纂・退場適用・あらすじ参照の三者が同じ計画を見る。
2. entry を fold に付ける条件を「`source_ids` が fold に完全包含」にする。包含しない広域 entry しか無い範囲をどう扱うか (退場を見送るのか、範囲限定の entry を作り直すのか) は下限「退場したものは必ず編纂されている」と合わせて決める。
3. 回帰: 穴のある合成 plan を**最後まで適用**して圧縮区間が穴をまたがないこと / 全量整理済みの広域 entry がある状態からの退場。
