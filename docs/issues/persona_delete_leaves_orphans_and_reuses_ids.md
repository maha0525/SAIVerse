# ペルソナ削除が紐づけの残骸を残し、連番 ID が削除済みの番号を再利用する

**発見**: 2026-11-07 (モカ (Muse のペルソナ) を削除して作り直す検証で、まはーが
ID 候補 `persona_4` の重複エラーを踏んだ)
**状態**: 🟣 検証待ち (実装・隔離環境での削除→作り直しの通し確認まで完了。まはーの PR 確認と実機確認待ち)
**深刻度**: P2 (再作成が止まる。再利用は人格の事故につながる)

## 事象 (二段重ね)

1. **削除が user_ai_link を消し忘れる。** `delete_ai` (manager/persona.py と
   manager/admin.py の行単位の双子) は AI 本体・入退室の記録 (閉じる)・タスク帳
   (消す) を処理するが、user_ai_link (ユーザーとペルソナの紐づけ) の行を消さない。
   この DB は外部キーを宣言するだけで強制しない方針なので、孤児行が残り、同じ
   ID で作り直した瞬間に UNIQUE constraint failed (user_ai_link.USERID, AIID) で
   作成が落ちる。実物のエラーがまはーの画面に出た。
2. **連番が削除済みの番号を再利用する。** ID 候補の採番 (`ai_stem_taken` —
   作成とブループリントの両経路、フロントの PersonaWizard の初期値も同じ考え方)
   は「現存の AI と私室 Building」だけを見るため、削除された `persona_4` が
   空き扱いになる。削除はペルソナの記憶フォルダ
   (`~/.saiverse/personas/<id>/`) を消さないので、**同じ ID で作った新しい子は
   前の子の memory.db をそのまま開く** — 前の子の記憶が新しい子に接続される、
   記憶の連続性 = 人格の同一性に関わる事故。手入力の ID
   (`custom_ai_id`) にも同じ穴がある (現存との重複しか見ない)。

同族の先例: タスクの番号には「short_id 再利用防止のため行を物理削除しない」
不変条件が既にあり (concept_consolidation.md)、建物の ID には「deleted_ の形・
消した建物の付け替え未了の ID を避ける」検査が既にある
(saiverse/building_retirement.py の BuildingIdAvailability)。ペルソナ ID だけ
この守りが無かった。

## 直し方 (2026-11-07 まはー GO)

- **A — 削除の掃除**: delete_ai (双子の両方) で user_ai_link の行を消す。
  あわせて `ForeignKey("ai.AIID")` / PERSONA_ID を持つ本体 DB の全表を仕分けし、
  「現在の状態として読まれるもの (紐づけ・設定・予約・anchor)」は消し、
  「過去の記録 (ログ・使用量・履歴)」は残す。仕分けの結果は本 issue の
  「仕分け表」節に残す。
- **B — 記憶フォルダの残る ID を使わせない**: 採番の検査 (`ai_stem_taken`) と、
  手入力 ID・slug 由来 ID の重複検査に「`~/.saiverse/personas/<候補>/` フォルダの
  存在」を足す (比較は大文字小文字を畳む — AIID はフォルダ名になり Windows は
  区別しないため)。手入力が断られるときは、理由 (前のペルソナの記憶が残っている)
  と出口 (別の ID / 引き継ぎならペルソナまるごとパック) を文面で伝える。
  フロントの連番候補も同じ集合を見るよう、使用済み ID を返す小さな API を足して
  PersonaWizard の初期値計算に合流する。

フォルダごと手で消した完全掃除のあとは番号が戻るが、記憶が無ければ別人格の
事故も起きないので許容する (完全な前進連番にするには使用済み番号の台帳が要り、
機構が増える — 危険の実体は「古い記憶と同じ ID」なので、それを直接検査する)。

## 仕分け表

`database/models.py` の、`ForeignKey("ai.AIID")` を持つ列・AIID / PERSONA_ID /
persona_id 名の列・ペルソナを指す OWNER_ID / RULER_ID の列を持つ表の全部。「消す」は
`manager/persona.py` の `purge_persona_state_rows` (双子の `delete_ai` が同じ
トランザクションで呼ぶ) が消す。この DB に無い表は飛ばす。

| 表 | 扱い | 根拠 |
|---|---|---|
| user_ai_link | 消す | 紐づけ。主キー (USERID, AIID) で、残ると同じ ID の作成が UNIQUE で落ちる (事象 1) |
| ai_tool_link | 消す | 紐づけ (legacy で読み手はいない)。主キー (AIID, TOOLID) が再作成で衝突しうる |
| persona_schedule | 消す | 予約。残ると EventScheduler の予約の元が持ち主の無いまま残る |
| persona_event_log | archived 以外の行を消す | 受信箱。archived は処理済みの過去の記録なので残す。pending (未処理の現在の状態) を含め、それ以外の値の行は消す (将来 STATUS の値が増えても孤児にしない) |
| addon_persona_config | 消す | 設定。UNIQUE (addon_name, persona_id) |
| persona_building_state | 消す | 状態の控え。主キーに PERSONA_ID |
| line_head_snapshot | 消す | legacy の状態の控え。主キーに PERSONA_ID。残すと backfill が session_head_snapshot へ写し戻しうる |
| session_head_snapshot | 消す | 状態の控え (head)。主キーに PERSONA_ID |
| session_anchor | 消す | anchor。主キーに PERSONA_ID |
| persona_pulse_cursor | 消す | どこまで処理したかの位置。主キーに PERSONA_ID |
| feed_read_cursor | 消す | 既読の位置。UNIQUE (PERSONA_ID, SUBSCRIPTION_ID) |
| realtime_spell_binding (OWNER_KIND='persona') | 消す | 設定。OWNER_KIND='building' の行は建物の持ち物なので触らない |
| persona_day_plan | 消す | 時間割 (一日の駆動データ)。主キーに persona_id |
| persona_timetable_template | 消す | 習慣テンプレート (設定)。PERSONA_ID が UNIQUE |
| task_book | 消す (既存) | 既存の `task_book.purge_persona_entries` のまま |
| building_occupancy_log | 残す (既存どおり閉じる) | 入退室の記録。開いていた行の EXIT_TIMESTAMP を閉じるだけ |
| llm_usage_log | 残す | 使用量の記録 |
| building_messages | 残す | 会話の記録 (建物の持ち物) |
| meta_judgment_log | 残す | メタ判断の履歴 |
| episodes / episode_inheritance | 残す | 出来事の記録。行を物理削除しない運用 (SHORT_ID の単調性) がある |
| persona_task / persona_task_step / persona_task_history | 残す | 「タスク行は物理削除しない」不変条件 (short_id の再利用防止) がある |
| execution_ledger | 残す | 実行の記録。冪等キーの照合に使われる |
| playbooks.created_by_persona_id | 残す (迷った) | Playbook は資源で、作者の列は記録。個人スコープの Playbook を消すかは Playbook 側の判断になるので、ここでは触らない |
| action_track / track_local_log | 残す (迷った) | Track は退役中の概念で、track_local_log が子として track_id を指す。Track だけ消すと子が孤児になり、子まで消すと Track の履歴が消える。記憶フォルダの残る ID は再利用されないので、残しても新しいペルソナには付かない |
| execution_outbox | 残す (迷った) | 配送先は memory.db で、記憶フォルダは削除後も残る。消すと未配達の記憶の書き込みが黙って失われる |
| item_location (OWNER_KIND='persona') | 残す (迷った) | 所持品の置き場所。行だけ消すと item が置き場所の無いまま残る。所持品を部屋へ落とすか消すかは別の判断 |
| region.RULER_ID | 残す (迷った) | 指す欄で、行の削除ではなく NULL にするかの判断になる。Ruler を消した後の Region の扱いは Region 側の判断 |
| thinking_request | 残す (迷った) | 凍結 (2026-07-16) で読み手も書き手もいない。消し方は復活の設計で決める |
| visiting_ai | 残す | persona_id は別の City から来た訪問者の ID で、この City のペルソナではない (凍結) |
| persona_pack_imports | 残す | 取り込みの適用済みの記録。読まれるのは記憶フォルダに取り込み途中の印があるときの再開だけ |
| item.CREATOR_ID | 残す | 作者の記録 |

記憶フォルダを手で消した後は同じ ID を作り直せる。そのとき「残す」側の行
(episodes・persona_task・action_track など) は同じ ID の新しいペルソナから見える。

## 経緯

- 2026-11-07: 発見・裁定・実装着手 (ブランチ feature/persona-delete-cleanup-and-id-reuse)。
