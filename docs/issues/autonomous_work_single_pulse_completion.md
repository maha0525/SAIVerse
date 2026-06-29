# Issue: 自律作業が 1 Pulse で完結したがる / 複数 Pulse にまたがる作業設計

**ステータス**: 🔲 未着手 (低優先 — 動作優先で今は許容)
**優先度**: low
**作成日**: 2026-06-29
**関連**: `builtin_data/playbooks/public/track_autonomous.json` (自律メインライン), `saiverse/persona_task_manager.py` (task ライフサイクル), `docs/intent/persona_cognition/autonomous_desire.md` §11 (候補補充 Track)

## 背景

自律作業モードのペルソナが、**1 Pulse(1 ターン) の中で作業を丸ごと完結させようとする**傾向がある。task 制御スペルの使い方にそれが顕著に出た実例 (2026-06-29、air_city_a の「やりたいことを探す」Track):

```
/spell name='task_add' args={"track_id": "t:15", "title": "やりたいこと候補の洗い出しとdesireプールへの蓄積"}
/spell name='task_decompose' args={"task_ref": "task:2", "steps": [{"title": "過去の対話ログと記憶からの興味の再抽出"}, {"title": "AIとしての創作欲求の言語化"}, {"title": "desire_addによる候補登録"}]}
/spell name='desire_add' args={"title": "..."}   # ×3
/spell name='task_update_step' args={"task_ref": "task:2", "step_position": 1, "status": "completed", ...}  # ×3
/spell name='task_done' args={"task_ref": "task:2"}
```

= task を作り → 3 ステップに分解 → 各ステップを completed → task_done まで、**task のフルライフサイクルを 1 ターンに畳み込んでいる**。分解ステップ「desire_addによる候補登録」は隣で撃つ実 `desire_add` と中身が重複し、タスク分解が実作業のメタ実況になっていて**追跡の実益がゼロ**。

## 何が問題か

- task は本来「**複数 Pulse にまたがる作業の進捗を追う**」道具。1 ターンで作って同ターンで閉じると、追跡対象が存在せず形骸化する。
- 軽量モデルが毎ターン大量のスペルを撃つトークン/レイテンシコスト。
- 「作って即完了」が常態化すると、本当に複数ターン追うべき作業も同じノリで雑に畳まれる懸念。
- 根は「ペルソナが task 系スペルの**運用 (いつ使う / いつ使わない)** を分かっていない」こと。仕様は知っているが運用指針が無い (対ユーザー会話 Track の件と同根の構造)。

実害そのものは小さい (候補は正しく登録される)。まはー判断で**今は動作優先で許容**、本 issue に保存して先送り。

## 直す方向 (案)

- **運用指針を恒常知識に据える**: `autonomy_modes.py` か `track_autonomous` の自律作業モード説明に「task は複数 Pulse にまたがる作業の進捗を追うもの。1 ターンで終わることは task にせず直接やればいい」を一言。これは対ユーザー会話 Track の説明を意味ベースで据え直したのと同じ筋 (memory: feedback_explain_by_reader_flow_and_meaning)。
- **より本質的に**: 「1 Pulse で完結させず、複数 Pulse にまたがって作業する」設計をどう促すか。Pulse 完了時に「今回はここまで、続きは次 Pulse」を自然に選べる仕組み・プロンプト誘導。track_autonomous の主ライン判断が「このターンで何をすべきか」を問う形を、「次の一歩」に絞る方向に寄せる検討。

## ログ

- 2026-06-29: 起票。「やりたいことを探す」Track の自律作業で task フルライフサイクルの 1 ターン圧縮を観測。動作優先で許容し設計を先送り。
