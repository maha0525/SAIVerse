# 記憶・人格境界 一次監査

**開始日**: 2026-07-12

**状態**: 指摘あり・一次監査完了（2026-07-13）
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

上記の続査対象は第2〜6片ですべて確認済み。一次監査は「全行読破」ではなく、記憶の生成・検索・退役・移植・共有転記を跨ぐ境界と主要実行入口のcoverageを完了した、という意味で2026-07-13に閉じた。

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

- sticky台帳のmodel切替影響は、thread越境修正のキー設計にmodelを含めるべきかという設計判断として追う。

## Findings（第4片: Metabolism並走・生ログ退役）

### [P1] Chronicle生成にpersona単位の共通排他がなく、同じ土地を重複編纂できる

- 場所: `sea/session_lifecycle.py:619-698,742-1008,1232-1244`, `sea/runtime_context.py:147-205`, `sea/gold_panning.py:588-705`, `api/routes/people/config.py:180-257`, `api/routes/people/arasuji.py:731-935`, `sai_memory/arasuji/generator.py:1158-1312`, `sai_memory/arasuji/storage.py:393-462`
- 事実:
  - Chronicle生成には、通常Pulse後のMetabolism、anchor失効時のpre-response生成、TTL session-closeのdaemon thread、UIの`organize-memory`、Chronicle生成APIのbackground jobという独立した入口がある。
  - session-closeの`_gold_panning_close_inflight`はclose同士だけを抑止する。通常Metabolism・手動整理・background jobとの共通排他はない。
  - `generate_unprocessed()`は既存Lv1の`source_ids`を先に読み、その後LLMを呼び、最後に新規行を作るcheck-then-insertである。土地の同一source集合に対するUNIQUE制約もない。
  - lifecycle系は共有`adapter.conn`を直接使い、Chronicle生成全体を`adapter._db_lock`で囲まない。Chronicle生成APIは同じmemory.dbへの別connectionを開くため、adapterのRLockだけを足しても全入口は直列化されない。
- 最小再現: 一時memory.dbへ同じ`source_ids=['m1','m2']`でLv1を2回`create_entry()`すると、例外なく2行が保存された（実測: `first`/`second`の2行）。並走する2生成器が双方とも事前readで未処理と判定すれば、この状態へ到達できる。
- 影響: 同じ生ログから内容の異なるChronicle/Fragmentが複数生成され、以後の統合・自動想起・写真参照が二重化する。並走時の共有connection操作は、重複以外にtransaction state競合を起こす可能性もある。
- 修正方針: memory.db単位（実質persona単位）の編纂coordinatorを設け、全入口を同じ排他へ通す。プロセス内Lockだけでなく、別connection/background jobも含むDB leaseまたはSQLite上の原子的claimを使う。source集合の重複をDB側でも拒否できる冪等キーを持たせる。
- 必要な回帰: 通常Metabolism・session-close・`organize-memory`・生成APIをbarrierで同時開始し、同じmessage IDが複数のGeneral Lv1へ所属しないこと。失敗したclaimが再試行を塞がないこと。

### [P1] Chronicleが無効・失敗・拒否でもanchorを進め、生ログだけをコンテキストから退役させる

- 場所: `sea/session_lifecycle.py:619-698,742-914`, `tests/test_gold_panning.py:415-442`
- 事実: `run_metabolism()`はChronicleトグルOFF、Memory Weave無効、生成例外、ユーザー拒否/timeoutのいずれでも、生成結果を確認せずstep 3でanchorをlow watermark位置へ進める。その後の完了通知は常に「N件の会話をChronicleに圧縮」と表現する。既存テストもChronicleを無効化した状態でanchorが`m3`へ進むことを実測・固定している。
- 影響: 生ログ自体はmemory.dbに残るため物理欠落ではない。しかし、まはーが指摘した「自動的に生ログがコンテキストから抜ける唯一の経路」で、代替地図が作られないまま土地だけが通常Sessionから不可視になる。通知は実状態と一致しない。
- 修正方針: 「編纂成功」と「Session窓の退役」を別結果として記録し、通常は必要な地図生成が成功したbatchだけを退役させる。Chronicleを意図的に無効化した仕様を維持するなら、非圧縮退役を明示的な別モード・別通知として扱い、後から未編纂範囲を追跡できるマーカーを残す。
- 必要な回帰: 成功・toggle OFF・LLM例外・拒否・cancelの各ケースで、anchor、未処理source、通知内容が一致すること。失敗後の再試行で退役範囲を取り戻せること。

### [P1] TTL失効時のminimal load後に旧anchorを再touchし、実際に送ったprefixと永続anchorが食い違う

- 場所: `sea/runtime_context.py:134-217`, `sea/session_lifecycle.py:109-171,237-307`
- 事実: `resolve_metabolism_anchor()`が有効anchorを見つけられないCase 3では、minimal tailを読み込むが`history_manager.metabolism_anchor_message_id`を新しいtail先頭へ更新もclearもしない。直後のLLM成功時、`touch_anchor_after_llm_call()`はhistory managerに残る旧anchorを読み、implicit cache（またはcache read/writeが観測されたexplicit cache）ではその旧IDを新しい時刻でDBへ保存する。
- 最小再現: history managerを旧anchor=`old`、DB anchorを期限切れにしてresolveすると`(None, 'minimal')`になり、in-memory値は`old`のまま。その後touchを呼ぶと更新対象IDは`old`になる（コード経路上決定的）。
- 影響: 当該callはminimal tailで新しいprefixを作ったのに、次回は古いanchorから長い履歴を読む。prompt cacheの寿命境界とSessionの生ログ窓が一致せず、一度退役した範囲が不意に復帰するほか、head snapshotのcache判定も誤る。
- 修正方針: minimal loadで実際に採用した最古message IDをそのmodelの新anchorとして確定し、LLM成功時は「今回組成したprefixのanchor」をcall-localに渡してtouchする。personaの可変フィールドから後読みにしない。
- 必要な回帰: 期限切れanchor→minimal load→LLM成功後、永続anchorがminimal tail先頭になること。並走model/callが互いのcall-local anchorをtouchしないこと。

## Findings（第5片: import / snapshot restore / migration）

### [P1] native importがtarget personaへthread identityを写し替えず、source persona名義を別人格DBへ保存する

- 場所: `api/routes/people/native_export_import.py:135-187`, `saiverse_memory/native_export.py:272-386`, `scripts/import_saimemory_native.py:78-151`
- 事実:
  - APIはURLの`persona_id`をtarget DBの選択にだけ使い、upload内の`persona_id`との一致を検証しない。
  - `import_threads_native()`は各`thread_data['thread_id']`と`resource_id`を無加工でtarget DBへ保存する。CLIもsource/target不一致を警告するだけで、`--new-thread`を指定しない限り同じ挙動になる。
  - 実測: source=`alice`の`alice:main`をtarget=`bob`へimportすると、bobのmemory.db内にthread/messageが`alice:main`名義のまま保存された。
- 影響: target personaのcanonical thread解決（通常`bob:*`）からimport済み会話が外れ、人格DBの中に別人格名義の土地が同居する。export元へ戻したように見えるthread IDが、実際には別personaの物理DBにあるという二重の不整合になる。
- 修正方針: native formatの「同一personaへの復元」と「別personaへの移植」を分離する。同一persona復元はsource/target不一致を拒否。移植はthread ID・resource ID・Stelis parent IDなどpersona prefixを原子的にtargetへ写像し、元IDをprovenance metadataへ保持する。
- 必要な回帰: alice→alice復元、alice→bob移植、Stelis親子を含む複数threadで、target DB内の全identityがbobへ閉じ、source provenanceだけが別欄に残ること。

### [P1] native importのreplaceがmessage単位commitで、失敗時に旧threadを失った部分適用を残す

- 場所: `saiverse_memory/native_export.py:272-386`
- 事実: replaceは最初に既存threadをDELETEし、その後thread作成、overview、Stelis、各messageを個別commitする。import全体を囲むtransaction/stagingはない。
- 最小再現: 既存`alice:main`に`OLD`を保存し、1件目は正常、2件目のmetadataをJSON化不能にしたarchiveをreplace importした。実測結果は`TypeError`で失敗し、`OLD`は消失、1件目`NEW1`だけが残った。
- 影響: statusは失敗を返すが実DBは元状態でも完成状態でもない。再実行できれば最終的に収束する場合はあるが、元threadは既に失われており、upload自体に欠陥があると回復できない。
- 修正方針: upload全体を事前validateし、target DB内のstaging tableまたは単一transactionへ全threadを書き、全件成功後にreplaceをcommitする。embedding生成はcommit後の再構築可能な派生工程へ分離する。
- 必要な回帰: 2件目・2thread目・Stelis復元・embeddingでそれぞれ失敗させ、既存targetがbyte/row単位で不変であること。成功時だけ全threadが一度に切り替わること。

### [P2] native importのembedding準備がarchive先頭threadからpersona IDを推測し、別personaのmemory.dbを生成し得る

- 場所: `saiverse_memory/native_export.py:389-438`, `saiverse_memory/adapter.py:82-111`
- 事実: `_regenerate_embeddings()`はtarget `persona_id`を引数で受けず、先頭`thread_id`のコロン前をpersona IDとみなして`SAIMemoryAdapter(persona_id)`を新規作成する。Adapter初期化はそのIDのpersona directoryとmemory.dbを作る。
- 影響: alice archiveをbobへimportすると、embedding用設定を得るだけのために`personas/alice/memory.db`を作成し得る。aliceが実在してもbobのimportがalice側DBを初期化・migrationし、実在しなければ孤児persona directoryを残す。
- 修正方針: target側の既存Adapter/embedderを明示的に渡すか、target persona IDと検証済みpathから非生成的にembedderだけを構築する。thread IDからpersona IDを推測しない。
- 必要な回帰: cross-persona import後にtarget以外のfilesystem treeが一切変化しないこと。悪意ある/壊れたthread IDでもworkspace外・personas root外へ書かないこと。

### [P1] snapshot restoreがarchive全体の検証・staging前に現状態を消し、展開失敗で部分復元を残す

- 場所: `scripts/snapshot.py:241-263,398-477`
- 事実: restoreは`clear_for_restore(home)`で現状態を削除してから、ZIP memberをhomeへ直接逐次extractする。事前に読むのは`snapshot.json`だけで、全memberのCRC/readability・必要ファイル・展開完了は検証しない。失敗時の自動rollbackもない（既定では復元前snapshotへの手動復旧手段は作る）。
- 最小再現: 現状態に`personas/current/memory.db`を置き、2 memberのsnapshotを復元中、2件目extractを失敗させた。実測: return code 1、current DBは消失、1件目だけ展開された部分状態が残った。
- 影響: 復元失敗の直後にSAIVerseを起動すると、欠けたworld/persona集合を正規状態としてmigration・初期化する可能性がある。auto snapshotは回復材料だが、失敗したrestore自体の原子性は保証しない。
- 修正方針: archiveを別staging directoryへ全展開し、CRC・manifest件数/サイズ・必須DBのSQLite integrityを確認してから、homeの対象集合と原子的にswapする。swap失敗時は旧treeへ自動rollbackする。
- 必要な回帰: CRC破損、途中I/O例外、容量不足、必須DB欠落、swap失敗で、旧homeが不変または自動復帰すること。成功時に除外対象logs/backups/snapshotsが保持されること。

### 移行経路で確認済み（新規findingなし）

- `Note → Theme page`は旧note UUIDを新ページID/metadataへ刻み、page作成済み・main DB削除失敗からの再実行も冪等に収束する。未移行noteが残る間は旧4テーブルをDROPしない。
- `vivid → desk`は同一memory.db transaction内でdesk追加とvivid印の解除をcommitし、本人が後で閉じたページを再起動時に開き直さない。
- 回帰実測: `python -m unittest tests.test_note_theme_migration tests.test_p4c_vividness_removal` — 27件成功。pytestは当該Python環境に未導入だったためunittestで実行した。

## Findings（第6片: sticky model境界 / RemotePersonaProxy / Building転記）

### [P1] Building→個人記憶の転記がcursorを先行確定し、memory.db書き込み失敗後に再試行されない

- 場所: `builtin_data/tools/get_building_messages.py:299-445`, `sea/runtime.py:137-143`
- 事実:
  - `auto_ingest_building_messages()`は未読候補を集めた直後、各messageを個人記憶へ書く前に`pulse_cursors[building_id] = max_seen_seq`を実行する。
  - その後の`history_manager.add_to_persona_only()`や`_mark_ingested()`はmessage単位の`try/except`内にあり、失敗をDEBUGログだけで握り潰して処理を継続する。cursorの巻き戻し・pending marker・再試行キューはない。
- 最小再現: seq=1の他persona発話をheard_by=`listener`で用意し、`add_to_persona_only()`を意図的に例外化した。初回はingest 0件だがcursorは`room:1`へ前進。2回目は`seq <= last_cursor`で除外され、書き込み関数自体が再度呼ばれなかった（実測: call数は1のまま）。
- 影響: Buildingの土地には残るが、そのpersonaのmemory.dbと以後の通常Sessionから発話が永久に欠落する。Metabolism以前の入口で起きるため、Chronicle/Fragmentにも編纂されない。
- 修正方針: messageごとに「個人memory.dbへのappend成功→Building側ingested marker成功」を確認してから、連続成功した最大seqまでcursorを進める。失敗seq以降は次回再試行する。より堅牢にはbuilding message IDを個人messageのprovenance/idempotency keyとして保存し、append成功・marker失敗後の再試行でも重複しないようにする。
- 必要な回帰: 途中messageのappend失敗、mark失敗、DB lock、プロセス再起動で、欠落も重複もなく最終的に全heard messageが取り込まれること。

### [P1] RemotePersonaProxyの思考転送が本番経路へ接続されず、訪問personaが応答・記憶形成できない

- 場所: `saiverse/remote_persona_proxy.py:1-32`, `manager/runtime.py:365-395`, `sea/pulse_controller.py:278-326,452-464`, `database/api_server.py:104-153`, `manager/background.py:23-74`
- 事実:
  - concept (`docs/concepts/sds.md`) は「Proxyがhome cityの`/persona-proxy/{id}/think`へ転送」と定義するが、`RemotePersonaProxy`は属性を置くだけで転送/thinkメソッドを持たない。
  - repository内で`/persona-proxy/{id}/think`へrequestを送る本番callsiteは0件。存在するのはAPI定義だけである。
  - user入力の`_build_responding_personas()`は`self.personas`（resident）だけを引き、`visiting_personas`を除外する。PulseControllerの`all_personas`経路へ直接proxyを渡しても、SEARuntimeが要求する`history_manager`、model、system instruction等を持たないためresident経路は成立しない。
  - home側のthinking request processorもSEA/HistoryManagerを通さず`persona.llm_client.generate()`を直接呼び、request/responseをpersona memoryへ保存しない。
- 影響: multi-cityを有効化して移動自体が成功しても、訪問personaは到着先の会話へ応答できず、仮にAPIを外部から直接呼んでも遠隔経験は本人のmemory.dbへ残らない。設計上の「同じ人格が別Cityを訪れる」が、現在は占有表示だけのproxyになる。
- 修正方針: visitor専用の明示的なPulse transportを作り、destinationで収集したcanonical contextをhomeのPulseController/SEAへ渡す。home側で本人のthreadへ入力・応答・訪問先provenanceを保存し、返答はdestination Building履歴へpersona ID付きで一度だけ書く。resident用PersonaCore interfaceへ不完全proxyを紛れ込ませない。
- 必要な回帰: City AのpersonaをCity Bへ派遣し、Bのuser発話→AでのSEA実行→Aのmemory.db保存→BのBuilding発話保存→再起動後の継続を2City test DBで固定する。timeout/重複pollでも返答を二重保存しないこと。

### [P1] destination Buildingのheard_by生成からvisitorが除外され、訪問中の土地自体にもaudience記録されない

- 場所: `manager/runtime.py:365-395,428-444,549-569`, `manager/visitors.py:292-316`, `builtin_data/tools/get_building_messages.py:299-445`
- 事実:
  - visitor IDは`occupants`には追加されるが、user発話保存時の`heard_by`は`responding_personas`（residentのみ）から作られる。
  - residentが一人以上いればuser発話はBuildingへ保存されるがvisitor IDはheard_byに入らない。visitorしかいなければ`responding_personas`が空なので、user発話そのものをBuildingへ保存しない。
  - residentのAI発話はemittersが`occupants`全体をheard_byに使うためvisitor IDも入るが、proxyにはHistoryManager/auto-ingestがなくhome memoryへ転記されない。この非対称により「聞いた」土地と「本人が保持した」記憶が一致しない。
- 影響: 将来transportだけを復活させても、destination contextをheard_by基準で組むとuser側の発話がvisitorから欠落する。セッションログのviewer filter上も、その場にいたvisitorが聞いていない扱いになる。
- 修正方針: Buildingへの事実記録のaudienceは応答能力ではなくoccupancy snapshotから作る。誰が応答するか（resident/visitor transport可否）と、誰がその場で聞いたかを別集合にする。visitorだけの部屋でもuser発話をBuildingの土地へ一度保存する。
- 必要な回帰: resident+visitor、visitorのみ、transport timeoutの各ケースでheard_byが実occupantsと一致し、Building messageが欠落しないこと。

### 確認済み（新規findingなし）

- sticky自動想起の台帳は`(persona_id, thread_id)`共有のままでよい。内容はmodel固有のcache/snapshotではなく、同じpersonaが同じthreadで直近に想起した土地/地図の集合であり、通常のdefault model切替後も会話の認知的連続性を保つ。現行はCONVERSATION root lineもpersona単位1本で、model同時並走の独立会話Sessionは実装されていない。将来同一threadで複数CONVERSATION Sessionを真に並走させる場合だけsession key追加を再検討する。
- Building→resident個人記憶の正常系では、他personaのassistant発話をlistener側へ`role='user'`＋話者名prefix＋`metadata.with=[speaker_id]`として保存する。listener本人名義のassistant発話には変形せず、自己著者性は保たれている。

## 直前レビューから継続する関連指摘

Memory Atlas P4-a には別文書で P1×3（fold契約不一致、split本文保存則違反、編纂の非原子性）を記録済み。これらも「記憶・人格境界」行の未解決findingとして扱う。

→ [2026-07-12 concept consolidation code review](2026-07-12_concept_consolidation_code_review.md)

## 次の監査片

一次監査完了。集計は **P1×18 / P2×2**（直結するAtlas編纂レビューP1×3を含む）。うち **P1×7 / P2×1 は修正・回帰固定済み**、P1×2はまはー裁定で現状仕様として保留、残りは修正待ち。次はP0サブシステム `migration / upgrade / backup` へ移る。
