# 記憶・人格境界 一次監査

**開始日**: 2026-07-12  
**状態**: 指摘あり・一次監査継続  
**監査軸**: 生ログの不変性 / persona・thread境界 / 自己著者性 / 参照整合性 / 可逆性

## 今回の coverage（第1片）

確認済み:

- per-persona `memory.db` のパス解決と `SAIMemoryAdapter` 取得
- `messages` の会話窓・範囲読み出し
- UI/API からの生ログ編集・削除経路
- 写真・SCENE が生ログを参照する入口
- 直前の Memory Atlas P4-a レビュー結果との接続

未確認（続査対象）:

- 自動想起のsticky台帳とmessage/Chronicle側の検索scope（Fragmentの削除境界は第2片で確認済み）
- head snapshot内の各Section / Desk / core memory の内容分離（snapshot物理キーのmodel分離欠落は第2片で確認済み）
- Metabolism / Chronicle / entity extraction の書き込み主体と二重実行
- import/export、backup/restore、note/theme/desire migration の全経路
- RemotePersonaProxy、他ペルソナ発話の取り込み、Building共有履歴から個人記憶への転記

## Findings

### [P1] SCENE・範囲写真の会話窓が thread を跨いで他文脈を混入する

- 場所: `sai_memory/memory/storage.py:1829-1863`, `1867-1941`
- 事実: `get_conversation_window_around()` はアンカーの `thread_id` を取得せず、前後を `rowid < ?` / `rowid > ?` だけで検索する。`get_conversation_messages_between()` も両端の rowid 範囲だけを検索する。除外条件に thread 一致はない。
- 最小再現:
  1. 同じ persona の memory.db に `thread-a:A1`、`thread-b:B1`、`thread-a:A2` の順で実会話行を入れる。
  2. A1 周辺窓と A1〜A2 の範囲を読む。
  3. 実測結果はいずれも `A1, B1, A2`。thread-b の発話が混入した。
- 影響: SCENE（人格の口調・関係性アンカー）や範囲写真へ別threadの会話が「過去の実際のやり取り」として刻まれる。人格境界と土地参照の正確性を同時に破る。
- 修正方針: アンカー（範囲は両端）の thread_id を取得し、同一threadでなければ拒否する。前後・範囲SQLへ `thread_id = ?` を必須条件として加える。
- 必要な回帰: 交互に挿入された2threadで、window/rangeの双方がアンカーthreadだけを返す。異なるthreadの両端指定は `None` または明示エラーになる。
- **修正済み (2026-07-12)**: 窓・範囲SQLに `thread_id = ?` 必須化。異thread両端は `None` (呼び出し元 memory_atlas が「区間は現在読み出せません」に落とす既存経路)。回帰は test_core_memory_scene.py::ConversationThreadBoundaryTest。

### [P1] 「不変の土地」である messages をUI/APIから直接改変・削除でき、派生記憶との参照整合も更新されない

- 場所: `api/routes/people/memory.py:165-195`, `saiverse_memory/adapter.py:856-932`, `frontend/src/components/memory/MemoryBrowser.tsx:346-385`
- 事実: MemoryBrowser はメッセージ本文・時刻の PATCH と物理DELETEを公開している。adapter は `messages` を直接 UPDATE/DELETEする。削除時に消すのは message embedding とmessage行だけで、photos、Chronicle source_ids、PageEditHistory参照等の派生参照を検査・更新しない。
- 影響:
  - 写真が写す土地の内容が後から別内容へ変わる、または参照先を失う。
  - Chronicle・編集来歴・コア記憶SCENEの出典が欠落する。
  - 「生ログは不変の地面」「地図は土地を偽造しない」という Atlas の根本規律が、通常UI操作で破れる。
- 修正方針: 生ログの通常編集・物理削除を停止する。訂正が必要なら、元行を保存したまま correction/tombstone を別層に記録し、表示・想起側で適用する。既存利用者の意図（インポート修正、誤記訂正、プライバシー削除）を分類してから代替導線を設計する。法的・プライバシー上の完全削除は、参照グラフを含む明示的な破棄操作として通常訂正と分離する。
- 必要な回帰: 通常訂正後も元本文と写真の由来が保存されること。完全削除時は全参照先を列挙し、孤児参照を残さないこと。
- **まはー裁定 (2026-07-12)**: 現状は仕様として後回し。課題として保持し、修正には着手しない。

### [P2] 共通 `get_adapter()` が存在しない・未ロードの persona ID を受け入れて memory.db を新規作成する

- 場所: `api/routes/people/utils.py:8-31`, `saiverse_memory/adapter.py:82-95`, `saiverse/data_paths.py:313-315`
- 事実: `get_adapter()` は `manager.personas` に対象がいなくても `SAIMemoryAdapter(persona_id)` へfallbackする。adapterは `<SAIVERSE_HOME>/personas/<persona_id>` を検証せず `mkdir()` し、memory.dbを初期化する。多くのpeople memory endpointはpersona存在確認なしでこのhelperを使う。
- 影響: typoや古いIDへのAPI呼び出しが「404」ではなく、人格本体の無い孤児memory.dbを生成する。persona_idの形式・解決先が検証されないため、ファイル境界の責務もhelperごとに不統一になる。
- 修正方針: 通常API用helperは `manager.personas` またはmain DBのAI実在を必須にし、不在なら404。オフライン保守で未ロードpersonaのDBを開く用途は、検証済みcanonical pathを使う別helperへ分離する。persona IDを単一のvalidatorで検証し、解決後パスがpersonas root配下であることを確認する。
- 必要な回帰: 不明IDでディレクトリが作成されず404になる。ロード済み・DBに実在するオフラインpersonaの扱いを仕様どおり固定する。`..`、区切り文字、絶対パス風IDを拒否する。
- **修正済み (2026-07-12、コミット c1bb7c4)**: validate_persona_id 単一 validator + get_adapter の実在必須化 (404) + helper 非経由の import 系3エンドポイントにも同関所。保守経路 (scripts/bootstrap/ツール) は現状維持。回帰 `tests/test_people_get_adapter.py`。

## Findings（第2片: 自動想起 / head snapshot）

### [P1] head snapshot が model ごとに分離されず、別モデルが同じ凍結headを共有する

- 場所: `sea/head_pipeline/pipeline.py:71,122,349-355`, `database/models.py:531-548`, `sea/head_pipeline/integration.py:68-90,260-300`
- 事実:
  - コメントと `LineHeadInput` は head を `(persona, model)` 単位と定義し、snapshot自体も `model_key` を持つ。
  - しかしin-memory keyは `(persona_id, line_id)`、DB主キーも `(PERSONA_ID, LINE_ID)` の2列だけ。`MODEL_KEY` は非キーの上書き列である。
  - `ensure_snapshot()` は既存snapshotの `model_key` と現在のctxを比較しない。
- 最小再現: 同じ `persona_id='air'`, `line_id='main'` で先に `model-a`、次に `model-b` のctxを `ensure_snapshot()` へ渡すと、2回目のsnapshotも実測で `model-a` のままになった。
- 影響:
  - モデル切替直後のLLMが、別モデルのcache TTL・capture時点に属するheadを受け取る。
  - 複数model並走時、一方のMetabolism/captureが他方のsnapshotを上書きする。
  - `landscape.md` が定義する `(persona_id, model_key)` 単位のSession分離と一致しない。
- 判断が必要な点: 本当にheadをmodel別に持つか、全model共有へ設計を改めるか。**推奨はmodel別**。anchor・cache TTL・Sessionが既にmodel別であり、headだけ共有すると寿命境界が一致しない。
- 修正方針（model別を採る場合）: pipeline/store/DBの物理キーを `(persona_id, model_key, line_id)` にする。全アクセサへmodel_keyを通し、既存行は記録済み `MODEL_KEY` を新主キーへ移行する。
- 必要な回帰: 同一persona/lineのmodel A/Bが異なるsnapshot/version/TTLを保持し、片方のcapture・discard・diff通知が他方を変えない。
- **まはー裁定 (2026-07-12)**: 現状は仕様として後回し。課題として保持し、修正には着手しない。

### [P1] ごみ箱へ移したMemopediaページのFragmentが自動想起に残り続ける

- 場所: `sai_memory/memopedia/core.py:383-425`, `sai_memory/unified_recall.py:216-266,749-783`
- 事実: ページ削除は `memopedia_pages.is_deleted=1` のsoft-deleteで、Fragmentは保存する。ページembedding検索は削除済みを除外するが、Fragmentのembedding検索・keyword検索には親ページの `is_deleted` 条件がない。
- 最小再現: `is_deleted=1` のページ、その配下Fragment、Fragment embeddingを1件ずつ作り `get_fragment_embeddings()` を呼ぶと、削除済みページ名と本文を含むhitが返った。
- 影響: ユーザーまたはペルソナが「ごみ箱へ移した」知識が、自動想起として本人のtailへ再注入される。削除の意味論・訂正導線・プライバシー期待を破り、削除済みページURIへの壊れた深掘り導線も生成する。
- 修正方針: Fragment検索は `memopedia_pages` を必須JOINし、`COALESCE(p.is_deleted,0)=0` をkeyword/embedding両経路へ共通適用する。孤児Fragmentの扱いも明示する（通常想起から除外を推奨）。復元すれば親フラグが戻り、自然に再び検索対象になる。
- 必要な回帰: active→soft-delete→restoreの各段階で、page hitとfragment hitが同じ可視性になる。keyword/embedding双方を固定する。
- **修正済み (2026-07-12)**: 共有SQL断片 `_FRAGMENT_VISIBILITY_JOIN/WHERE` を keyword/embedding 両経路に適用。INNER JOIN 化で孤児 Fragment も除外。embedding 行は削除中も保持 (復元時に再計算不要)。回帰は test_unified_recall.py::TestFragmentVisibilityFollowsPage。

### [P1] 自動想起のmessage経路がline/scope/tag境界を無視し、内部ログをメイン会話へ戻し得る

- 場所: `sai_memory/memory/storage.py:477-493`, `sai_memory/unified_recall.py:270-291,790-821`, `sea/auto_recall.py:771-785`
- 事実:
  - message embeddingのバックフィル対象はcontent非空の全messageで、role/line_role/scope/tagによる除外がない。通常書き込み時もcontentがあればembeddingを作る。
  - `unified_recall()` のmessage keyword/embedding検索条件は実質 `role != 'system'` だけ。
  - 自動想起は `search_messages=True` でこの経路を使う。
  - したがって `line_role IN ('sub_line','meta_judgment','nested')`、`scope='discardable'`、role=userで保存されたevent_message等も候補になる。通常履歴とChronicleにはこれらの除外規則が既にある。
- 影響: サブラインの試行錯誤、破棄されたメタ判断、システム通知が「本人の過去の会話」と同じ顔でメインラインへ再注入される。内部思考と本人発話の名義境界、line隔離、短期通知と長期記憶の区別を破る。
- 判断が必要な点: 自動想起のmessageソースを「実会話のみ」にするか、内部ログも由来ラベル付きで想起可能にするか。**推奨は実会話のみ**。内部ログを想起させたい用途は、別source_typeと明示ラベルを持つ経路に分けるべきで、本人の会話記憶へ混ぜない。
- 修正方針: Chronicle/SCENEで共有している実会話フィルタを、埋め込み・keywordの両message検索へ適用する。embedding自体を全行分保持することは他用途のため許容できるが、自動想起の取得時scopeは必須。
- 必要な回帰: 同じ語を含むmain_line、sub_line、meta_judgment/discardable、event_messageを並べ、自動想起のmessage hitがmain_line実会話だけになることをkeyword/embedding双方で固定する。
- **修正済み (2026-07-12、裁定=実会話のみ)**: storage.py に `real_conversation_filter()` を新設 (SCENE の `_conversation_exclusion()` + 通常履歴の discardable 除外の合成、一点管理) し、message keyword/embedding 両経路に適用。副作用として UI の統合検索・scene 検索も実会話のみになる (意図どおり)。回帰は test_unified_recall.py::TestMessageSearchRealConversationOnly。

### [P1] sticky自動想起の台帳がthreadを跨ぎ、別threadへ直前の想起を持ち込む

- 場所: `sea/auto_recall.py:121-168,698-863`, `sea/runtime_context.py:464-501`, `saiverse_memory/adapter.py:1418-1444`, `api/routes/people/memory.py:211-221`
- 事実:
  - intentはsticky台帳を「ライン単位のインメモリ状態」と定義しているが、実装の `_LEDGERS` は `persona_id` だけをキーにする。
  - コメントは「CONVERSATIONメインラインはpersona単位で1本」と置く一方、実装にはactive thread切替APIとStelisのthread切替が存在する。
  - thread切替時に `reset_ledger()` を呼ぶ経路はない。リセットされるのは自動想起をOFFにした場合とテストだけである。
  - そのためthread Aで採用された項目は、thread Bで検索に再採用されなくても `stale_turns <= sticky_turns` の間は台帳に残り、thread Bの末尾へ全件注入される。
- 影響: 独立した会話threadの最初の数ターンへ、直前threadの話題・人物・生ログ断片が「ふと浮かんだ記憶」として混入する。threadを文脈分離面として使うStelisや手動thread切替で、粘着仕様が逆に境界漏れになる。
- 修正方針: 台帳キーを少なくとも `(persona_id, thread_id)` にする。将来CONVERSATION root lineが同一thread内で複数並走し得るなら `(persona_id, thread_id, line_id)` まで持つ。`run_auto_recall()` の呼び出し側はadapterから取得したcanonical thread_idを明示的に渡し、thread終了時または切替時の台帳破棄も用意する。
- 必要な回帰: thread Aでhitを採用後、同一personaのthread Bへ切り替えて検索hitを空にし、Aの項目がBへ注入されないこと。Aへ戻った場合に台帳を復元するか捨てるかは仕様を決めて固定する。
- **修正済み (2026-07-12)**: `_LEDGERS` を `(persona_id, thread_id)` キーに変更。呼び出し側が adapter の canonical thread_id 解決 (`_thread_id(None)`) を明示的に渡し、解決不能時は注入スキップ (安全側)。**仕様固定: A へ戻れば台帳は自然復元 (B 滞在中は老化しない)、明示リセットは全 thread 破棄、上限/掃除は設けない**。intent (memory_architecture_v2.md §4.3) に実装ノート追記済み。回帰は test_auto_recall.py::TestThreadLedgerIsolation。

## 未確定の調査メモ（finding未昇格）

- Metabolism本体は通常PulseではMetaLayerのper-persona直列化下にある。一方、cache TTL由来のsession closeはdaemon threadで走り、`_gold_panning_close_inflight` はclose同士だけを防ぐ。通常Metabolism・UIの`organize-memory`との共通排他、および共有SQLite connectionの`adapter._db_lock`適用は確認できていない。実際に並走可能な発火系列とDB操作を次片で追い、再現できた場合のみfindingへ昇格する。
- `run_metabolism()` はChronicle無効・生成失敗時にもanchorを進め、完了通知を「Chronicleに圧縮」と表現する。生ログは残るためデータ欠落とは断定しないが、通知と実状態の不一致として別途確認する。

## 直前レビューから継続する関連指摘

Memory Atlas P4-a には別文書で P1×3（fold契約不一致、split本文保存則違反、編纂の非原子性）を記録済み。これらも「記憶・人格境界」行の未解決findingとして扱う。

→ [2026-07-12 concept consolidation code review](2026-07-12_concept_consolidation_code_review.md)

## 次の監査片

1. Metabolism通常実行・session close・`organize-memory`・Chronicle手動生成の並走結果を、独立した再現ハーネスで追う。
2. import/export・backup/restore・各migrationで、人格IDと保存先の対応が崩れないかを追う。
3. sticky台帳のmodel切替影響は、thread越境修正のキー設計にmodelを含めるべきかという設計判断として追う。
