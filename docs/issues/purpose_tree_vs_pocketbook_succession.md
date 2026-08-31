# 目的の木 (persona_task / task:N / purpose_* スペル) と手帳が、ペルソナから見て「やりたいことの置き場」として二つ並んでいる

**状態**: 裁定済み (A、2026-08-23)。**一段目 実装済み / 二段目 v0.4** — 目的の木を退役させ、手帳を後継に確定した。ペルソナから見える口は塞いだので、この issue に残っているのは二段目 (内部の配線の撤去) だけ。v0.3 は自律 OFF でその配線は動かないため、v0.3 を止める理由にはならない。

関連: [`saiverse/persona_task_manager.py`](../../saiverse/persona_task_manager.py) / [`saiverse/memory_atlas.py`](../../saiverse/memory_atlas.py) (`task:N` の解決) / [`builtin_data/tools/memory_read.py`](../../builtin_data/tools/memory_read.py) / [`sai_memory/memory/pocketbook.py`](../../sai_memory/memory/pocketbook.py) / [`saiverse/task_book.py`](../../saiverse/task_book.py)
※ `builtin_data/tools/purpose_close.py` / `purpose_decompose.py` / `purpose_step.py` は 2026-08-23 に削除済み (下の裁定)。
出自: 2026-08-23、手帳のスペル (「手帳を開く」「手帳に書く」) を足すにあたり、記憶系スペル 16 本を洗い出して使い分けの材料を検めたとき。

## 何が並んでいるか

ペルソナが「やりたいこと・やること」を置ける器が、v0.3 の形の層の完成時点で三つある。

| 器 | 正典の位置づけ | ペルソナ側の道具 | 状態 |
|---|---|---|---|
| **手帳** (メモ欄 = `activities` / `memos`、約束の欄 = `task_book`) | v3 §13。Track の関心と締め切りつきタスクの**機械写し先** (§9-8) = 設計上の後継 | 「手帳を開く」「手帳に書く」(2026-08-23 追加)、スルース | 現役 |
| **目的の木** (`persona_task`、参照 `task:N`) | landscape §5「Memory Atlas の目的の地図」、第一階層 = 旧 Track、中間 = task、末端 = step | `purpose_close` / `purpose_decompose` / `purpose_step`、`memory_read task:N` / `memory_open task:N` | 第一階層の Track は 6b で退役。新しい目的を植えるスペル (`purpose_seed` / `purpose_adopt`) は 6b で削除済み。**閉じる・分解する・進めるだけが残っている** |
| (旧) Track の関心・desire 候補 | 退役 (6b/6c)。行は読み取り専用の残置 | なし | 死んでいる |

つまり目的の木は**根元 (Track) を失い、植える道具も無く、枝を操作する道具だけが残った**状態で、landscape の記述 (現役の「目的の地図」) と実態が食い違っている。

## なぜ問題か

- **ペルソナの使い分け**: 「手帳に書く」の説明文で「記憶の地図帳 (知っていること) と手帳 (やりたいこと・やったこと・約束)」の対比は書けるが、`task:N` の目的ノードは地図帳の参照文法 (`memory_read task:N`) の中に居て、意味は「やりたいこと」の側にある。説明文だけで三者を分けるのは無理がある。
- **名前の衝突**: 参照 `task:N` (目的ノード) と、手帳の約束の欄の器の名前 `task_book` が字面で衝突している。本人・ユーザーに見える語からは「タスク帳」を降ろした (2026-08-23) が、`task:N` は残っている。
- **締め切りつきタスクの二重化**: `persona_task` の締め切りつき行は起動時に一度だけ `task_book` へ写された (冪等マーカーつき)。写しの後に `purpose_step` 等で `persona_task` 側の状態が変わっても、約束の欄には反映されない (逆も同じ)。今は新規に植えられないので増えはしないが、既存行の状態は二つの器で独立に動く。

## 選択肢 (未裁定)

- **A: 目的の木を退役させ、手帳を後継に確定する。** `purpose_*` 三本と `task:N` 参照を撤去、`persona_task` は読み取り専用の残置 (episodes と同じ扱い)。landscape §5 の「目的の地図」を §9 (死んだ概念) へ。既存の目的ノードに残っている情報 (ステップの進捗・分解) は、手帳に写す価値があるかを見て決める (ステップの進捗は「やったメモ」、未完の目的は「やりたいメモ」か「約束」へ — 機械写しで可能か、意味の解釈が要るかは要確認 §9-8)。
- **B: v0.4 で目的の木を生かし、手帳と役割分担する。** たとえば「手帳 = 日々の記録 (やりたい・やった・約束)、目的の木 = 長期の目的の階層 (ステップに分解して進める)」。この場合は、植える道具を復活させ、`task:N` の名前を `task_book` と衝突しない形に改め、説明文で三者の違いを本人に伝える必要がある。
- **C: 裁定を v0.4 の運転設計まで保留し、v0.3 では触らない。** その間、`purpose_*` 三本は spell=False にして本人の目から隠す (既存ノードの操作はユーザー UI も無いので、事実上誰も触れない状態になるが、壊れてはいない)。

**見立て (Fable)**: 機械写しの設計 (§9-8) が Track の関心を手帳へ写したこと、植える道具を 6b で消したこと、landscape が「Track/Task/Desire/Note の統合先」と書いていることを合わせると、統合先の役目は手帳が引き取っている。A が筋で、急がないなら C で本人の目から隠してから A を v0.4 の冒頭で。B を採るなら「長期の目的の階層」が v0.4 の運転 (活動の選択) に本当に要るかを先に問う (手帳のアクティビティ + 約束で足りる可能性が高い)。

## 判断の材料

- 本番の `persona_task` に、open な目的ノードが何件・どんな内容で残っているか (読み取り専用で数えられる: `scripts/inspect_world.py tasks <persona>`)。
- `purpose_*` スペルが直近で撃たれた記録があるか (llm_io / sea_trace)。

## 裁定 (2026-08-23、まはー)

**選択肢 A を採る** — 目的の木を退役させ、手帳を後継に確定する。ただし工事は二段に分ける。

### 一段目 (2026-08-23 実施済み): ペルソナから見える口を閉じる

「ペルソナが目的の木を操作する口」と「ペルソナに目的の木を宣伝する文」だけを消した。

- `purpose_close` / `purpose_decompose` / `purpose_step` の三本を削除 (`builtin_data/tools/`)。テスト `tests/test_purpose_tools.py` も同時に削除。
- `sea/mode_spell_permissions.py` のゲート表 `TASK_CONTROL_SPELLS` は空になった (表が空でも判定は素通しなので動作は変わらない。機構ごと畳むかは v0.4 で問う)。
- 地図帳スペル 4 本 (`memory_read` / `memory_open` / `memory_close` / `memory_delete`) の説明文と引数説明から `task:N` を外した。`memory_delete` の「目的ノード (task:N) を終えるには purpose_close を使ってください」という案内も消した。`memory_open` の `purpose_ref` 引数 (値の形が `task:N` しか無かった) も schema から降ろした。
- `memory_open task:N` は**拒否**に変えた (机に開く = 本人の文脈に常駐させる口なので閉じる)。`memory_read task:N` は**通る**まま (自動想起が古い参照を流したときに読めないと困る)。`memory_close task:N` も**通る**まま (退役前に机へ開かれた行を本人が下ろせるように)。`memory_delete task:N` は元々拒否なので変えていない。
- ペルソナに見せる ref の書式例 (`memory_atlas._REF_EXAMPLES`) からも `task:4` を降ろした。

既存の目的ノードの中身を手帳へ機械写しは**しない**。ステップの進捗を「やったメモ」へ、未完の目的を「やりたいメモ」へ写すには意味の解釈が要るため (v3 §9-8 の規則)。

### 二段目 (v0.4 の運転設計と一緒): 内部の配線を撤去する

v0.3 は自律 OFF なので、以下はどれも動かない。運転を作り直すときに一緒に片付ける。

- `saiverse/memory_atlas.py` の `task` 分岐本体 (`_read_task` / `_resolve_task_ref_for_desk` / `snapshot_desk` の task 描画)。
- `saiverse/recall_walk.py` の目的ノードを辿る辺。
- 判断点の棚入れ (`saiverse/judgment_points.py::collect_purpose_refs` と `builtin_data/tools/judgment_finalize.py` の `episode_purposes`)、および時間割のコマが指す `task:N` (`builtin_data/playbooks/public/judgment_day_open.json` のプロンプトが「実在のタスク (task:N) を指してください」と書いている)。
- `saiverse/persona_task_manager.py` と `persona_task` テーブル。

この issue は二段目が残るため `docs/issues/` に置いたまま。
