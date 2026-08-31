# SEA runtime / Session / head-tail 一次監査

**開始日**: 2026-07-15

**状態**: 指摘あり・一次監査完了
**監査基準**: `113567e`
**監査軸**: persona / thread / line / model隔離、Session境界、head snapshot、tail通知、Metabolismによる履歴縮小、cache anchorと実LLM利用の一致

## 現行Intentの再確認

確認済み:

- `docs/intent/session.md`
- `docs/intent/cached_head_architecture.md`
- `docs/intent/cache_lifecycle_control.md`
- `docs/intent/dynamic_state_sync.md`
- `docs/intent/persona_cognition/line_tag_responsibility.md`（現行§10 aspect）
- `docs/issues/session_lifecycle_extraction_design.md`

重要な正典:

- Session粒度は `(persona, model)`。同personaでもmodelが違えば別Sessionで、片方のMetabolism/head更新を他方へ波及させない。
- head snapshotは最低 `(persona_id, line_id)`、設計上はline×modelの組で独立し、model切替は再captureする。
- Aspectがline_role / scope / model tierを単一供給し、AUTONOMOUS/WORKERはlightweight、CONVERSATION/METAはstandardを使う。
- Metabolismは生ログが次回contextから自動的に外れる唯一の通常経路であり、どのmodelのwindowを狭めるかを誤ってはならない。

## Findings

### [P1] 実LLMモデルを無視して標準モデルのSession/anchorを更新し、モデル別Metabolismを成立不能にする

- 場所: `sea/runtime_runner.py:179-226`, `sea/runtime.py:463-565`, `sea/runtime_context.py:46-151`, `sea/head_pipeline/integration.py:62-145,260-350`, `sea/session_lifecycle.py:25-173,237-330,499-692`
- 事実:
  - LLM client選択はactive LineFrameのAspectを読み、AUTONOMOUS/WORKERでは `persona.lightweight_model`、CONVERSATION/METAでは `persona.model` を選ぶ。この部分は現行Intentどおりmodel tierを切り替える。
  - しかしcontext/headはLineFrameをpushする**前**に `_prepare_context()` で構築される。`render_head_messages()` はactive aspect/modelを引数に受けず、`build_line_head_input()` が常にpersonaのdefault/standard modelを `model_key` にする。line_idも常に`"main"`固定である。
  - `ensure_snapshot()` のin-memory/store keyは `(persona_id, line_id)`だけで、既存snapshotの `model_key != ctx.model_key` も検査しない。model_changedイベントを実LLM選択からdispatchする経路もない。
  - `resolve_metabolism_anchor()`、high/low watermark、cache hot判定はすべて `persona.model` を使う。LLM成功後は`UsageInfo.model`に実モデルが記録されているにもかかわらず、`touch_anchor_after_llm_call()`はそれを無視し、`persona.model`のcache種別・TTL・anchor・token thresholdを更新する。
  - `run_metabolism()`も新windowのanchorを `persona.model` にだけ保存し、`history_manager.metabolism_anchor_message_id` はpersona単位の単一属性として上書きする。したがってlightweight側の独立Sessionは一度も作られない。
- 最小再現:
  1. personaの標準modelを `standard-model`、成功したUsageのmodelを `lightweight-model`、現在anchorを `anchor-1` とする。
  2. `touch_anchor_after_llm_call()` を呼び、anchor更新先を記録する。
  3. 更新されたのは `lightweight-model` ではなく `standard-model / anchor-1 / TTL 1200秒` だった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_cache_lifecycle.py` に追加して実行し、誤ったmodel keyへのtouchを確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-model-anchor tests/test_cache_lifecycle.py::test_audit_lightweight_usage_touches_standard_model_anchor`
  - 結果: `1 passed`（usageはlightweight、更新先はstandardである壊れた状態を確認）。
- 影響:
  - lightweight呼出がstandard cacheを使っていないのに、standard anchorの `updated_at` を前進させる。次のstandard Pulseは実cacheが失効していても「TTL内」と誤認し、長大な生ログwindowと古いhead snapshotをcache hit前提で送る。
  - lightweight側はanchor・TTL・token thresholdを持てず、長いcontextモデルがまだ生ログを参照できるか、短いモデルだけwindowを狭めるべきかを区別できない。`session.md`の「片方の節目がもう片方へ波及しない」が構造的に不成立である。
  - Metabolismのhigh/low watermarkとtriggerもstandard設定で決まる。lightweightの上限を超えても縮小されない、またはlightweight都合の発火がpersona共通history anchorを動かし、他modelの生ログ可視範囲まで変える可能性がある。
  - keep-alive/session watchdogの予約keyも `ttl:{persona_id}` でmodelを含まず、複数modelの監視が互いに上書きされる。誤model touchと合成して、どのcache/Sessionを温存・終了しているか追跡不能になる。
- 修正方針:
  - Pulse開始時にAspectから実行modelを一度解決し、`ExecutionContext(persona_id, thread_id, line_id, aspect, model_key, pulse_id)`としてcontext構築・LLM選択・usage記帳・anchor・Metabolismへ同じ値を伝播する。各層で `persona.model` を再推測しない。
  - context構築前にLineFrameをpushするか、少なくとも同じ解決済みaspect/model/line IDを明示引数で渡す。client選択後に別modelへfallbackした場合は実 `usage.model` と照合してexecution contextを更新する。
  - head state/storeとSession anchorを `(persona_id, thread_id, line_id, model_key)` で分離する。model不一致snapshotを使い回さず、model_changed時は該当modelのsnapshotだけをcaptureする。
  - `history_manager.metabolism_anchor_message_id` のpersona単一可変属性を廃止し、execution単位で解決したanchorを保持する。Metabolismは対象modelのwindowだけを進め、Chronicle生成（persona共有）と履歴縮小（model別）を分離する。
  - TTL/watchdog keyにもmodel/lineを含め、各Sessionを独立監視する。
- 必要な回帰:
  - standard/lightweightが交互・並行に呼ばれても、head snapshot、anchor、TTL、token threshold、watchdogがmodel別に独立すること。
  - lightweight usageがstandard anchorを一切touchせず、standard cache失効判定を延命しないこと。
  - 一方のMetabolism後も他方が自身のanchorから同じ生ログを参照でき、他方が節目を迎えた時だけwindowが狭まること。
  - structured-output fallback等で選択modelが途中変更された場合も、実usage.modelとSession keyが一致すること。

### [P1] Chronicle生成失敗後もMetabolism anchorを前進させ、未圧縮の生ログをコンテキストから外す

- 場所: `sea/session_lifecycle.py:619-696`
- 事実:
  - `run_metabolism()` はGeneral ChronicleとTrack Chronicleの例外をそれぞれwarningだけで握り、処理を継続する。
  - その後、Chronicle生成の成否を確認せず `history_manager.metabolism_anchor_message_id` とmodel anchorを新しいwindow先頭へ更新する。
  - 完了eventは失敗を反映せず、常に「evict_count件の会話をChronicleに圧縮」と通知する。
- 最小再現:
  1. Memory WeaveとChronicleを有効化し、`generate_chronicle()` だけを例外にする。
  2. `m0..m4`、keep_count=2で `run_metabolism()` を実行する。
  3. Chronicleは1件も生成されていないのにanchorは `old -> m3` へ進み、completion eventもChronicle圧縮成功を名乗った。
- 実行確認:
  - 監査用の一時回帰を `tests/test_cache_lifecycle.py` に追加して実行し、再現後にテスト本体を削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-metabolism-chronicle tests/test_cache_lifecycle.py::test_audit_chronicle_failure_still_advances_anchor`
  - 結果: `1 passed`（Chronicle例外後もanchor=`m3`、status=`completed`、成功文言を確認）。
- 影響:
  - SAIMemory DB上の生ログ自体は直ちに削除されないが、通常コンテキストは新anchor以後しか読まない。失敗した圧縮対象は人格から見えなくなり、長期記憶にも残らない。
  - Metabolismは生ログが通常コンテキストから自動脱落する経路なので、この順序は一過性の補助処理失敗ではなく、会話連続性の欠落になる。
  - ログとUIは成功を報告するため、欠落を運用側が検知できない。
- 修正方針:
  - anchor前進を「必要な永続成果物がcommit済み」の後に置く。Chronicle必須時は生成・保存失敗ならanchorを据え置き、次回再試行する。
  - General/Track/Gold Panningの必須度は個別に定義し、少なくとも失敗内訳と「anchorを進めた／据え置いた」を構造化結果へ含める。
  - 完了eventは実成果に基づいて生成し、未圧縮時に成功文言を出さない。
- 必要な回帰:
  - Chronicle生成・Chronicle保存・model lookup・SAIMemory readの各失敗でanchorが前進しないこと。
  - 再試行で同じwindowが処理され、成功後に一度だけanchorが進むこと。
  - Chronicle無効化が明示設定の場合の設計（生ログだけを外してよいか）をintentで確定し、失敗と無効化を別状態として扱うこと。

### [P1] diff通知の配送前にlast_notifiedを進め、SAIMemory停止・push失敗時の世界変化を永久欠落させる

- 場所: `sea/head_pipeline/pipeline.py:227-307`, `sea/head_pipeline/integration.py:149-203`, `sea/head_pipeline/store.py:129-176`
- 事実:
  - `flush_diffs()` は差分labelを作った時点でin-memoryの `last_notified_sections` をnew snapshotへ進め、DBにも保存してからlabelを返す。
  - 呼び出し側 `inject_diff_notifications()` はその後にSAIMemory readinessを確認する。未readyなら「labels discarded」としてreturnする。
  - `push_perception()` の例外もlabel単位で握り、失敗数を返さず、最後は全件をpushした旨のinfoと `True` を返す。
- 影響:
  - SAIMemoryの一時停止やDB例外が起きた瞬間の入退室、所持品、Building、Memopedia等の変化は知覚bufferへ届かない一方、BだけがAへ進む。次回diffでは同じ変化を再検出できず、人格はその世界変化を永久に知覚しない。
  - partial failureでも成功件数が分からず、失敗labelだけを再試行できない。これは `B = A + Σ(events)` と「未消費知覚はbufferに残して再試行」というintentに反する。
- 修正方針:
  - diff検出とackを分離し、labelをdurable outbox/perception bufferへ全件commitできた後だけ対応SectionのBを進める。
  - batchに安定IDを持たせ、部分失敗時は未ack分だけ再試行する。`push_perception()` は受理可否を返し、未readyを成功扱いしない。
  - Bの永続化失敗も成功扱いせず、再起動後の重複はstable event IDで冪等化する。
- 必要な回帰:
  - SAIMemory未ready、1件目／途中／最終labelのpush例外、B保存例外、process再起動の各ケースで欠落も重複知覚も起きないこと。

### [P1] Stelis開始後の中断・例外でactive threadを親へ戻さず、以後の履歴を子threadへ誤保存する

- 場所: `sea/runtime_nodes.py:329-405`, `sea/runtime_graph.py:230-305`, `saiverse_memory/adapter.py:1406-1473`
- 事実:
  - `stelis_start` はpersona directoryの `active_state.json` を子threadへ切り替える。
  - 親へ戻す処理は後続の明示的な `stelis_end` nodeにしかない。
  - graphの例外・cancel `finally` はLineFrameとpulse log等を片付けるが、active threadの復元を行わない。
  - active threadはPulseContext局所値ではなくpersona共有ファイルであり、以後のhistory read/appendは毎回その値を読む。
- 影響:
  - Stelis区間内のLLM例外、tool例外、ユーザー割込み、cancelでplaybookが終わると、親会話へ戻ったつもりの後続Pulseまで子threadへ保存される。
  - 親threadの会話は途切れ、子threadの一時思考がmain会話として延命する。手動でthreadを戻すまで自然回復しない。
  - META_JUDGMENTはmain laneと並列実行されるため、mainがStelis中ならmeta側のhistory取得・保存も共有active threadに引きずられる。
- 修正方針:
  - threadをmutableなpersona-global fileからExecution/Pulse contextへ移し、すべてのread/appendへ明示的なthread_idを渡す。
  - Stelisはstack/token型context managerとしてpushし、成功・例外・cancelすべての`finally`で対応するparentへpopする。
  - process crash用にはactive Stelis ownership/pulse statusを永続化し、起動時にorphanを検出して親へ復旧する。
- 必要な回帰:
  - start直後、任意node、LLM待機中、end処理中のcancel/例外で必ず親threadへ戻ること。
  - main StelisとMETA_JUDGMENT並列時に、双方のread/write threadが交差しないこと。

### [P1] perception message保存失敗を成功扱いし、durable bufferを全削除する

- 場所: `saiverse_memory/adapter.py:388-455, 2132-2195`, `sea/runtime.py:143-156`
- 事実:
  - `flush_perception_buffer()` は `append_persona_message()` が例外を投げた場合だけbufferを保持する。
  - 実際の `_append_message()` はDB/embedding等の例外を内部で捕捉して `None` を返すため、通常の保存失敗は例外として届かない。
  - flush側は戻り値を検査せず、pending IDを全削除して `True` を返す。
- 最小再現:
  1. `world_state` perceptionを1件積む。
  2. `append_persona_message()` を実装契約どおり `None`（保存失敗）にする。
  3. `flush_perception_buffer()` は `True` を返し、SAIMemory messageを作らずpendingを0件にした。
- 実行確認:
  - 監査用一時回帰を `tests/test_core_memory_scene_api.py` に追加して再現し、確認後に削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-perception-loss tests/test_core_memory_scene_api.py::CoreMemorySceneApiTest::test_audit_flush_deletes_buffer_when_append_returns_none`
  - 結果: `1 passed`（append=`None`でもflush=`True`、pending消滅）。
- 影響:
  - 世界状態、移動先の様子、persona recall、コア記憶のユーザー訂正など、bufferへ安全に積んだはずの未消費知覚がDB瞬断・embedding失敗等で不可逆に消える。
  - callerも成功と判断するため再試行されず、人格は重要な訂正や環境変化を知覚できない。
- 修正方針:
  - appendを例外または明示Result型へ統一し、message IDの取得をcommit成功条件にする。`None`ならpendingを残し `False` を返す。
  - appendとpending deleteを同一DB transaction/outbox ackにまとめる。embedding失敗がmessage commitを巻き戻す設計なら、その状態もflush失敗として扱う。
- 必要な回帰:
  - append=`None`、DB commit例外、embedding例外でpendingが残り、次Pulseの成功時に一度だけ消費されること。

### [P1] head capture/render失敗時もLLMを実行し、persona identityなしの応答を履歴へ確定し得る

- 場所: `sea/head_pipeline/pipeline.py:85-129`, `sea/head_pipeline/integration.py:260-350`, `sea/runtime_context.py:46-92`, `sea/head_pipeline/store.py:53-127`
- 事実:
  - `capture_all()` はSection capture例外を握り、既存値がなければそのSection名をkeyに残したままvalue=`None`でfresh snapshotを作る。
  - `ensure_snapshot()` の欠損判定はkey集合しか見ないため、`None`のSectionを欠損と認識せず再captureしない。
  - renderは`None` Sectionを黙ってskipする。`common_prompt` / `persona_self` / `core_memory`でも同じ扱いである。
  - head pipeline全体が例外になっても `prepare_context()` はexception logだけで処理を続け、LLM呼び出しを止めない。
  - snapshot DB保存も例外を内部で握るため、Metabolism直後の保存失敗→process再起動では旧headをfresh TTL中のsnapshotとして復元する。
- 影響:
  - 初回captureの一時失敗だけで、persona promptやcore memoryを欠いた応答を生成・保存できる。これは単なる表示欠落ではなく、人格に属さない発話を本人の履歴へ混ぜる経路になる。
  - DB保存失敗後の再起動では、system prompt編集・記憶整理・Building状態の更新前headへロールバックし、anchor TTLが切れるまで自己修復しない。
- 修正方針:
  - required Section（最低でもcommon/persona self）のcapture/render/serialize/persistをfail closedにし、LLM実行前にhead readinessを検証する。
  - snapshotにSectionごとのstatus/error/versionを持たせ、`None`や欠損はfresh扱いせず再試行する。optional Sectionだけ明示的degraded modeを許可する。
  - store APIはcommit成否を返し、in-memory stateとpersistent stateの世代を一致させる。失敗時はdirty/retry stateを残す。
- 必要な回帰:
  - required Sectionの初回capture、再capture、render、serialize、DB commitの各失敗でLLMが呼ばれず、復旧後に同一Pulseを安全に再試行できること。
  - optional Section失敗時は欠落Sectionと警告が構造化され、次Pulseで自動回復すること。

### [P2] 秒精度timestampだけでanchor境界と履歴順を決め、同一秒のevicted prefixを再混入させる

- 場所: `sai_memory/memory/storage.py:384-444, 524-554`
- 事実:
  - messageの `created_at` は `int(time.time())` の秒精度で保存される。
  - `get_messages_from_id()` はanchor ID自身を境界にせず、そのIDのtimestamp以上を取得する。
  - ORDER BYも `created_at` だけでstable tie-breakerがなく、通常履歴のpaginationも同じである。
- 影響:
  - 同じ秒にanchorより前に書かれたmessageがすべてanchor以後として復活する。高速なtool roundやperception flushでは同一秒衝突が通常に起こり得る。
  - ページ境界の同一timestamp群は順序契約がなく、DB/query plan次第で重複・skip・turn順の揺れが起き得る。prefix安定性と厳密なMetabolism window境界を満たさない。
- 修正方針:
  - thread内単調sequence（または `(created_at_ns, row sequence)`）を正典の順序キーにし、anchor queryを「anchor rowのsequence以上」で切る。
  - すべてのhistory/Chronicle/pagination queryを同じtotal orderへ統一する。
- 必要な回帰:
  - 同一timestampにuser/assistant/toolを複数挿入し、任意anchorから正確にそのrow以後だけが同順序で返ること。
  - page boundaryを同一timestamp群の中央に置いても重複・skipしないこと。

### [P2] model anchorのread-modify-writeに直列化と保存成否がなく、並列Pulseで片方のSession状態を失う

- 場所: `sea/session_lifecycle.py:49-105, 173-233`
- 事実:
  - `update_anchor_for_model()` はAI行のJSON全体をloadし、1 modelを書き換え、JSON全体をsaveする。
  - row lock、version compare、atomic JSON updateのいずれもない。main laneとMETA laneは同一personaで並列になり得る。
  - `save_anchors()` はcommit例外をwarningだけで握り、呼び出し側へ失敗を返さない。
- 影響:
  - standard/lightweightが同時に古いJSONを読み、それぞれ保存するとlast writerがもう一方のanchor/TTLを消す。
  - DB保存に失敗してもwatchdog予約とsuccess logは進み、process内状態と再起動後のSession状態が分岐する。
- 修正方針:
  - anchorを `(persona_id, model_key[, thread_id, line_id])` の行へ正規化し、model単位upsertにする。少なくともoptimistic version/CASで競合を再試行する。
  - saveは結果を返し、touch・watchdog予約・成功logをcommit成功後だけ行う。
- 必要な回帰:
  - standardとlightweightの同時touchをbarrierで競合させ、両anchorが残ること。
  - commit失敗後にwatchdogが更新されず、次の成功で回復すること。

### [P2] token超過triggerをmessage件数の最小差分で拒否し、巨大少数messageをMetabolismできない

- 場所: `sea/session_lifecycle.py:499-591`
- 事実:
  - token threshold超過時に `_metabolism_token_triggered` を先にFalseへ戻す。
  - その後 `len(current_messages) - low_wm < 10` ならreturnし、token量を見ずMetabolismを行わない。
  - high/low watermarkも `persona.model` のmessage件数設定であり、少数の巨大messageを表現できない。
- 影響:
  - 画像説明・tool結果・長文user input等の少数messageだけでtoken thresholdを超えたSessionは、trigger済みでも縮小されない。次回も同じ巨大prefixを送り、context limit/費用問題が継続する。
- 修正方針:
  - token trigger時は件数差分gateを適用せず、token budgetから安全なanchorを選ぶ。最低evict件数は通常のmessage-count triggerだけに限定する。
  - trigger flagはMetabolism commit成功後にclearし、defer/失敗時は保持する。
- 必要な回帰:
  - low watermark未満の件数だがtoken threshold超過の履歴で、少なくとも1件を安全にevictしてanchorが進むこと。
  - Chronicle/anchor失敗時にtriggerが残り、次Pulseで再試行されること。

## Coverage

一次監査で確認した面:

- Pulse開始からcontext/head構築、Aspectによるmodel選択、usage確定後のanchor touchまでの順序
- `(persona, thread, line, model)` のkey伝播と、standard/lightweight・META並列の分離
- SAIMemory history filter、anchor query、timestamp ordering、in-memory fallback
- Metabolismの通常発火・token発火・Chronicle/Track Chronicle/Gold Panning・anchor更新・Dynamic State hook
- head snapshot capture/render/store/load/TTL refresh、diff検出、last_notified、perception buffer配送/flush
- Stelis start/endとgraphのsuccess/error/cancel cleanup
- explicit keep-aliveとnon-explicit session watchdogの予約key/更新契約

結論: **P1×6 / P2×3、一次監査完了**。とくにMetabolismとtail配送の「成果物commit前に不可逆位置を進める」共通パターン、ならびにSession keyが実行model/thread/lineまで届かない点を修正の中心に置く。

- thread_idとline_idの履歴取得・保存・head snapshot隔離
- Metabolism失敗時のanchor更新、Chronicle/履歴縮小の原子性
