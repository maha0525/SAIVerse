# Playbook 許可ゲートが覆っていない起動口と、`user_only` の二重の意味

**ステータス: 両件ともまはー裁定済み・完了** (2026-08-17 起票、同日裁定。① `user_only` = ペルソナに対する禁止と確定し実装済み / ② SUBPLAY は放置と確定)

## まはー裁定 (2026-08-17)

- **① `user_only` は「ペルソナに対する禁止」**。ユーザー本人が名指しした起動は通す。実装済み (`decide_playbook_permission` で `user_configured=True` なら `user_only` を通す。`blocked` は完全封印のまま常時拒否)。
- **ボタン文面**: 「以後使用しない」→「**ペルソナには使わせない**」。**「この」は付けない** — 設定は City 単位で保存され同じ街の全ペルソナに効くので、「このペルソナには」と書くと目の前の一人だけの話に見えて嘘になる (まはー指摘)。
- **② SUBPLAY は放置**。ペルソナが Playbook を書けるようになった時点で再考する (下記 ① の節が再開条件)。

**副産物の観察**: 設定画面 (`GlobalSettingsModal`) は既に `user_only` を「ユーザー指定時のみ」と表示しており、**画面の約束のほうが正しく、コードが守っていなかった**形だった。また「使用禁止」(`blocked`) は表示専用で UI から設定できない (API の `valid_levels` から除外) ため、**「二度と動かないでほしい」の受け皿は現状 UI に無い**。必要になったら別件。

前提として、City スコープの Playbook 許可 (`playbook_permission` テーブル: `blocked` / `user_only` / `ask_every_time` / `auto_allow`、**行が無いときの既定は `ask_every_time`**) の判定は 2026-08-17 に `SEARuntime.decide_playbook_permission` へ集約した。通っているのは Playbook の EXEC ノードと `/run_playbook` スペルの 2 口。

## ① SUBPLAY ノードは許可判定を通らない

`sea/runtime_nodes.py` の `lg_subplay_node` は子 Playbook をロードしたらそのまま `_run_playbook` を呼ぶ。したがって、ユーザーが `blocked` / `user_only` にした Playbook でも、別の Playbook の SUBPLAY ノードから起動できる。

**素直に同じゲートを通すと壊れる**: SUBPLAY の対象は Playbook JSON に静的に書かれた内部依存 (`meta_exec_speak` → `sub_speak` など)。行の無い Playbook の既定は `ask_every_time` なので、ゲートを通すと**内部ステップのたびに確認ダイアログが出る**。許可の仕組みは「ペルソナが *選んで* 起こす Playbook」を対象にしており、作者が書いたグラフの辺は対象外、という前提でできている。

**現状の到達性**: 現在の SUBPLAY の辺は `generate_image` (auto_allow) / `source_web` / `research_task` / `sub_speak` の 4 つで、`blocked` / `user_only` の Playbook を指しているものは無い。Playbook を書けるのは今のところユーザーとリポジトリだけ (`save_playbook` ツールはどの Playbook からも参照されておらず spell でもないため、ペルソナからは起動できない)。**ペルソナが Playbook を書けるようになった時点で、これは「ユーザーの禁止を迂回する経路」になる**。

**裁定 (2026-08-17 まはー): 放置**。案 (a) — 現状維持で、`save_playbook` をペルソナへ開放するときに再考する。ペルソナが Playbook を書けない限り、SUBPLAY の対象は人間が書いたグラフの辺であり、禁止の迂回にはならない。

## ② `user_only` が「ユーザーだけが起動できる」と「二度と使わない」を兼ねている

`user_only` は 2 つの意味を持っている。

- **UI が付ける意味**: ペルソナや自律実行からは起こせない = ユーザーが起こすなら良い
- **`never_use` が付ける意味**: 確認ダイアログで「今後使わない」を選ぶと `_set_playbook_permission(..., "user_only")` が書かれる = 無効化

そのため、ユーザーがチャット UI の「ツール指定」やスケジュール設定画面で `user_only` の Playbook を**自分で名指ししても拒否される**。前者の意味からは通すべきで、後者の意味からは拒否すべき。

**確認ダイアログの文面 (まはー指摘 2026-08-17)**: `frontend/src/components/PlaybookPermissionDialog.tsx` は終始「ペルソナが実行しようとしている」枠で書かれている — 見出し「Playbook実行の確認」、その下に「{ペルソナ名} が実行しようとしています」。したがって「以後使用しない」ボタンは**「このペルソナには以後使わせない」**と読むのが素直で、書き込まれる値 `user_only` (= ユーザーだけが起こせる) の名前とも一致する。**文面まで含めると天秤は片側に傾き、「ユーザー自身が名指しした起動なら通す」が妥当**。

残る引っかかり: ボタンは「以後使用しない」としか言っていないので、「もう二度と動かないでほしい」のつもりで押した人には、後から自分のスケジュールで動くのが約束破りに見える。塞ぐならボタンの表示を「このペルソナには使わせない」に寄せる (表示名と実際の効果を一致させる方向)。

**裁定 (2026-08-17 まはー): ペルソナに対する禁止**。`user_configured=True` のときは通す。ボタン文面も同時に「ペルソナには使わせない」へ変更した (「この」なし)。新しい値の追加も migration も不要。

## 関連

- `sea/runtime.py` `decide_playbook_permission` (判定の正典) / `sea/runtime_nodes.py` `lg_subplay_node` (未接続の起動口)
- [W10 走行メモ](../handoff/2026-08-16_w10_spell_audit_remnants_handoff.md) — ゲート集約の経緯
- [pulse_type_not_inherited_by_subplaybooks.md](pulse_type_not_inherited_by_subplaybooks.md) — 隣接 (`call_playbook` の要否)
