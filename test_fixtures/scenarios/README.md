# 一日シミュレータのシナリオファイル

`scripts/run_day_sim.py` に渡すシナリオの置き場。書式の正典は
`saiverse/day_scenario.py` のモジュール docstring（本 README は使い方の要約）。

```bash
# mock（既定・LLM コストゼロ・配線確認）: DB もペルソナも自動で仮設される
python scripts/run_day_sim.py --scenario test_fixtures/scenarios/day_standard.json

# 実 LLM（コスト発生）: テスト環境の DB とペルソナを使うこと（本番に向けない）
python scripts/run_day_sim.py --scenario <file> --real --city city_a --db-file test_data/user_data/database/saiverse.db
```

出力は**一日新聞と生データの対**（レビューは必ずセットで見る）:
- 新聞: `--out` 先（省略時 `~/.saiverse/personas/<id>/day_reports/<date>.md`）
- 生データ: 新聞と同じ場所の `<date>_raw.md`（判断点・SAIMemory 全文・建物メッセージ・
  移動・タスク/欲求・時間割・アイテム）。`--raw-log-out` で場所変更、`--no-raw-log` で抑止

mock シムの不変条件は回帰スイート `tests/test_day_sim_regression.py` が守っている
（全判断点 submitted / 全コマ終端 / 成果物実在 / 予算内 / 新聞・生データ生成）。
自律行動まわりを変更したらまずこれを回すこと。

## フィールド

| フィールド | 必須 | 意味 |
|---|---|---|
| `persona_id` | ✓ | 一日を生きるペルソナ。mock では自動作成される。`--real` では **その DB に実在するペルソナ ID** に書き換えること |
| `plan_date` | — | 「今日」の日付 (YYYY-MM-DD)。省略時は実行日の日付 |
| `wake` / `sleep` | ✓ | 起床判断・就寝判断の仮想時刻 (HH:MM)。日跨ぎ（sleep < wake）は未対応 |
| `daily_budget_rounds` | — | 日次予算（セッションのラウンド総枠）。省略時 40 |
| `seed.desires[]` | — | 一日の種になる欲求。`type` は六型（話す/聞く/作る/知る/経験する/自分を更新する）、`source` は出自（実経験への参照。接地の証跡として保存される） |
| `seed.tasks[]` | — | 種になるタスク。`goal` は完成条件（セッション指示書に載る） |
| `user_events[]` | — | ユーザーの動き。`{"at","type":"message","text"}` で会話開始、`{"at","type":"leave"}` で退室（→会話終了判断が走る）、`{"type":"absent_all_day"}` で終日不在 |
| `events[]` | — | 世界のイベント。`{"at","description","is_alert"}`。`is_alert: true` は即応対（engage_now）に縮退する |

## 同梱シナリオ

- `day_standard.json` — 標準の一日。種の欲求2＋タスク1、15時にユーザー会話の割り込み、18時にイベント。「起床→図書館→工房→会話→就寝」の全経路が通る
- `day_absent.json` — 終日不在＋空バックログ。何も起きない日が静かに（クラッシュせず）終わることの確認用

## `--real` の前提

1. テスト環境を用意（`python test_fixtures/setup_test_env.py`、環境変数は `SAIVERSE_HOME=test_data/.saiverse` / `SAIVERSE_USER_DATA_DIR=test_data/user_data` の test_fixtures 流儀）
2. 判断点 playbook 5 本をその DB に import：
   `python scripts/import_playbook.py --file builtin_data/playbooks/public/judgment_day_open.json`（day_open / post_session / post_conversation / on_event / day_close の 5 ファイル）
3. `persona_id` を実在ペルソナに書き換え、`LIGHTWEIGHT_MODEL` が設定されていることを確認（セッション実行は軽量モデル）
4. 施設を使いたい場合は Building にロールタグを付与（任意。無ければ own_room で全部進む）:
   `sqlite3 <db> "UPDATE building SET FACILITY_ROLES='[\"library\"]' WHERE BUILDINGID='<id>';"`
   語彙: `plaza`（話す/聞く）/ `workshop`（作る）/ `library`（知る）/ `park`（経験する）。複数可 `'["plaza","park"]'`
