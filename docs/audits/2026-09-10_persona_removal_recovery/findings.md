# 削除の周辺で見つかった不具合の候補

調査日 2026-09-10 / 対象コミット `25ad75d6`

本調査の主題 (削除後の記憶の再利用と無効化の成立性) とは別に、経路を辿る過程で
見つかったもの。**方式の選択には直接必要ないが、どの方式を選んでも残る。**

各項目に、**私が根拠パスを開いて確かめたか / 実験で観測したか / 読んだだけか**を書いた。

---

## A. 名前の頭が一致するだけで、削除できないペルソナが生まれる

**確認済み** (`manager/admin.py:1762-1777` を読んだ)

削除の拒否条件 `_is_seeded_entity` は DB を見ず、ID の頭文字だけで判定している:

```python
seeded_prefixes = ["air_", "eris_", "genesis_", "luna_", "sol_",
                   "user_room_", "deep_think_room_", "altar_of_creation_"]
return any(entity_id.startswith(prefix) for prefix in seeded_prefixes)
```

ペルソナの ID は `<名前から作った文字列>_<都市>` の形になるので、
**利用者が別の都市で「Luna」を作ると `luna_city_b` になり、永久に削除できない。**
「Sol」「Air」「Eris」も同じ。エラーは `"Error: Seeded AIs cannot be deleted."` で、
利用者には理由がわからない。

---

## B. Ruler ペルソナを消すと、その区画が二度と消せなくなる

**確認済み** (`manager/admin.py:845-848` と `RULER_ID` の全参照を追った)

Region の削除は、`RULER_ID` が入っていると拒否する:

> `Remove the Ruler first.`

ところが **`RULER_ID` を空に戻すコードがリポジトリのどこにも無い。**
書き込むのは `saiverse/saiverse_manager.py:2011` の 1 箇所だけ。
`delete_ai` もこの列を触らない。

つまり Ruler を削除すると、Region は「Ruler を先に消せ」と言い続けるのに
その Ruler は既に居ない、という詰みになる。

---

## C. 手帳の書き出し・読み込みを往復させると、そのペルソナが喋れなくなる

**実験で再現** (経路担当が隔離環境で観測。私は仕組みの側を確認した)

Memopedia の書き出し (`sai_memory/memopedia/core.py:948-963`) が運ぶのは
id / parent_id / title / summary / content / category / 日時だけで、**`metadata` を運ばない。**
一方コア記憶の識別子 `core_id` は、その `metadata` の中にある
(`sai_memory/core_memory.py:127` の `meta["core_id"] = core_id`)。

往復させると `core_id` の無いページができ、`list_core_memories()` が `TypeError` を送出する。
この関数は発話の準備で `required = True` の区画から呼ばれ、読み取り例外を意図的に
握り潰さない設計なので (`sea/head_pipeline/sections/core_memory.py:87-116`)、
**そのペルソナの発話が止まる。**

**この欠陥の型は、同じファイルで既に一度直っている。** すぐ上の行に、Chronicle を
書き出しから除外する処理があり、理由がこう書いてある:

> Chronicle エントリは export しない — level / source_ids / short_id が metadata JSON に
> あり、この形式では運ばれない (import しても壊れた entry しか復元できない)。
> (2026-08-19 Codex 第五巡 #1)

**metadata が運ばれないことを認識して Chronicle は手当てしたが、
同じ理由が当てはまるコア記憶は手当てされていない。**

---

## D. 別のペルソナへ記憶を移す機能が、画面から呼べない

**確認済み** (`frontend/src/components/memory/MemoryImportForm.tsx:81` を読んだ)

別 ID への移植 (`transplant`) は API とライブラリに実装されているが、
画面はこの指定を一度も送らない。結果、利用者から見るとこうなる:

1. 別のペルソナの記憶ファイルを選ぶ
2. 画面が警告を出す — 「元のペルソナ (X) とインポート先 (Y) が異なります。
   **スレッドIDはそのまま保持されます。**」
3. インポートを押す
4. 400 が返る — 「別 persona へ移すには transplant (移植) を明示指定してください」
5. **画面にその指定手段が無い**

2 の文言も実際の挙動と違う。別 ID への「復元」は書き込む前に拒否されるので、
スレッド ID が保持された状態にはならない。

---

## E. 消したペルソナのアラームが残り、二通りの形で回り続ける

**読んだだけ** (経路担当の調査。私は再登録の経路までは追っていない)

削除は `persona_schedule` を消さないので、行が残る。その後の挙動が二つに割れる:

- **起床・就寝 (判断点)**: `saiverse/autonomy_wiring.py:91` の
  `AUTONOMOUS_DRIVING_SHIPPED = False` に先に当たり、正常な前進として
  静かに毎日回り続ける (失敗しない)。
- **利用者が作った通常のアラーム**: ペルソナが見つからず失敗し、
  再試行を使い切り、翌日ぶんを登録し直す。**毎日失敗し続ける。**

再登録は起動時だけでなく、60 秒周期の回復処理からも走る
(`saiverse/schedule_manager.py:305`)。
**どちらも利用者に見える経路が無い。**見えるのはバックエンドのログだけ。

---

## F. 削除しても消えないもののうち、性質が違う 3 つ

**確認済み** (`database/models.py` を読み直した / 実験で観測)

削除後に残る 25 テーブルのうち、単なる残骸と言えないもの:

- **`addon_persona_config`** — アドオンの設定。**外部サービスの認証情報を含む。**
  ペルソナを消しても残る。
- **記憶のバックアップ** — `~/.saiverse/backups/` の 2 か所にあり、
  `personas/<id>/` の外。**フォルダを消しても記憶は残る。**
  (これは §削除の設計では利点にもなる。§README の判断 2 を参照)
- **`active_state.json`** — 「今どのスレッドを開いているか」を持つファイル。
  DB を消しても残る (実験 3 で観測)。**DB とファイルで消え方が揃っていない。**

---

## G. `create_ai` と `delete_ai` に、到達しない複製がある

**確認済み** (`manager/persona.py:697,707` と呼び出し元を追った)

`manager/persona.py` に `create_ai` (697) と `delete_ai` (707) があり、
実際に呼ばれるのは `manager/admin.py` 側 / `saiverse/saiverse_manager.py` 側。
`delete_ai` の docstring 自身が「AdminService 側の同名定義と行単位の複製関係にある —
片方を変えたらもう片方も揃えること」と書いている。

`docs/issues/archive/persona_mixin_ai_edit_dead_duplicate.md` は
`get_ai_details` / `update_ai` の複製を 2026-08-12 に撤去した記録。
**同じ掃除が `create_ai` / `delete_ai` に届いていない。**

---

## H. `CLAUDE.md` の記述が古い

**確認済み** (`persona/tasks/storage.py:3-6` を読んだ)

`CLAUDE.md` の Memory Stack にこう書いてある:

> **Task storage** (`persona/tasks/storage.py`) — per-persona `tasks.db`.

実際は 2026-06-28 に main DB の `persona_task` テーブルへ一本化済みで、
per-persona の `tasks.db` は廃止されている。モジュールの docstring 自身がそう書いている。

**この記述は、今回の調査で「ペルソナに紐づくものはどこにあるか」を数えるとき
実際に誤りの元になった。**
