# ペルソナの記憶がペルソナのディレクトリで完結しない（可搬性の負債）

**起票**: 2026-07-11（Memory Atlas P3c の X 案裁定に伴い、まはー指摘）
**種別**: アーキテクチャ負債（後回し確定・凍結ではない）
**関連**: `docs/intent/concept_consolidation.md` P3c / inter-city travel（VisitingAI）/ 将来のペルソナ引っ越し・エクスポート

## 問題

ペルソナの記憶・状態が `~/.saiverse/personas/<id>/`（memory.db 等）だけで完結せず、**main DB（saiverse.db）側に散在している**。このため「ペルソナを丸ごと持ち運ぶ」操作（別 City への訪問・引っ越し・エクスポート/インポート・バックアップ復元）が、ディレクトリコピーで済まず main DB との縫い目の移送を伴う。

main DB 在住のペルソナ帰属データ（2026-07-11 時点の主なもの）:

- **目的の木**: `persona_task` / `persona_task_step` / `persona_task_history`（P3c X 案裁定により物理格納は main DB のまま。task:N 参照・ファサード・写真・机は memory.db 側の機構と ref 文字列で接続）
- **Note 系**: `note` / `note_page` / `note_message` / `track_open_note`（P3c で Note→テーマノードページ移行後は縮小見込み）
- **Track**: `action_track`
- **出来事**: `episode`
- **判断ログ**: `meta_judgment_log`
- **時間割・予算**: `persona_day_plan` 等
- **AI 行そのもの**: システムプロンプト・LIFE_PURPOSE・感情状態・ACTIVITY_STATE 等

## 判断の経緯

- P3c（Memory Atlas 物理統合）で persona_task を memory.db ページへ移す案（Y 案)を検討し、**X 案（物理移動しない）で裁定**（まはー、2026-07-11）。外から見える便益がほぼ無い（ref ベースで単一アドレス空間は達成済み）ため
- **障壁の正体**（夜間監査 `docs/handoff/2026-07-11_p3c_purpose_note_audit.md` で事実確認）: 当初想定した FK/JOIN/トランザクション整合は**実は障壁ではない**（FK は実行時未強制・実 JOIN 無し・テーブル跨ぎトランザクション無し）。本当のコストは ①PersonaTaskManager/NoteManager の呼び出し元 約40箇所の構築パターン変更（SessionLocal → per-persona conn 解決）②main DB の1表 → N 個の per-persona memory.db への**扇形移行**（P3a/3b の「adapter init で一回きり」流儀が使えず、全ペルソナを触り切るまで完了しない）
- ただし「だいぶ気持ち悪い」（まはー）——概念上は一つの地図帳、実装上は二棟のまま
- **本 issue の本質は目的の木の置き場に限らない**: Y 案を敢行しても episode / meta_judgment_log / AI 行等は main DB に残るため、可搬性問題は完全には解けない。解くなら「ペルソナ帰属データの完結性」を軸にした独立の設計（per-persona DB への大移動 / エクスポート・バンドル形式 / main DB は運行状態のみ持つ、等）が要る

## 発火条件（この issue を取り出すタイミング）

- inter-city travel の本格運用再開（SDS 復活・multi-city）を計画するとき
- ペルソナの引っ越し・エクスポート/インポート機能を設計するとき
- ペルソナ単位のバックアップ/復元の完全性が問題になったとき

## メモ

- rdiff-backup による memory.db バックアップは既存だが、上記 main DB 側は saiverse.db 全体バックアップに混在しており「このペルソナの分」を切り出せない
- 参照アドレッシング（task:N 等の ref 文字列）は DB 跨ぎに中立なので、将来の移動時も ref 不変条件は保てる見込み（P3a/P3b の移行で実証済みの手筋）
