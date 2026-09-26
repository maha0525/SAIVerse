# 過剰安全設計によるコード膨れ 監査 (2026-09-26)

> **ステータス**: 監査結果のみ。コードは一行も変更していない(まはー指示: 成果物は本書のみ)
> **目的**: コードレビュー→直しの反復で堆積した「過剰な安全設計の層」を洗い出し、スマート化の候補台帳にする
> **関係**: [`docs/overview/architecture_health.md`](../../overview/architecture_health.md) とは補関係で重複なし — あちらは構造負債(巨大ファイル・循環 import・神ページ)、**本書は冗長な防御層の堆積**という別角度
> **方法**: 6 サブシステム(sea / saiverse / api+manager / 記憶スタック / llm_clients+tools / frontend)を並列監査。全所見は候補ごとに callee・caller を実読して検証した者だけを掲載。看板所見 5 件(§2 の ★ 付き)はメティスが直接再検証済み
> **規律の根拠**: `docs/issues/audit_20260730_review_guards.md`(レビュー前提の独立検証)· CLAUDE.md「例外経路を精巧にする前に供給源を塞げ」· CLAUDE.md「getattr default が返ったら名を疑え」

---

## 1. 総合評価

**診断: 膨れは実在する。ただし「カオス」ではなく、レビュー往復が積んだ特定パターン 5 種の堆積として定型化できる。** 放置の害は行数そのものより、**セクション 2 の fail-open ガード**(発火した瞬間に守る対象を黙って手放す向きに壊れるもの)と、**幽霊ガードによる誤診の温床**(タイプミスが「None 扱いで静默停止」に変換される)にある。

機械スキャンの規模(実測値・事実):

| 領域 | except | getattr | 備考 |
|---|---|---|---|
| `sea/` | 445 | 100+ (うち約半数が自家オブジェクト向け) | except→即 return None は 10 箇所のみ(残りはログ付き) |
| `saiverse/` | 627 | 383 (うち manager 常設属性向け ≈94) | 台帳なし縮退の並行実装 ≈400 行 |
| `api/`+`manager/` | ~270 | 176 | 同型 HTTP 500 変換 48、rollback ボイラープレート 53 |
| 記憶スタック | ~300 | 33 | バックアップ機構 4 本・keep 管理 3 実装 |
| `llm_clients/`+`tools/` | — | gemini.py 91(大半は SDK 応答向け=正当) | リトライ二重積み 6 経路、builtin tools の manager-None ガード 43 箇所 |
| `frontend/src` | catch 418 | — | mountedRef 乱立・SSR ガード乱立の仮説は**外れ**(実測 0 / 7) |

削除・圧縮で消える行の目測: api/manager 単体で 550〜700 行、台帳縮退 ~400 行、LLM 互換チェーン ~150 行、履歴 no-op 群 ~140 行、他断片多数。**ただし本書は全面一掃の推奨ではない**(§6 の着手原則)。

---

## 2. 最優先クラス — 発火すると fail-open するガード(単なる膨れより危険)

レビューで積まれた盾のうち、**「盾が働いた瞬間に盾の本体が死ぬ」向きに壊れているもの**。ここは行数削減の話ではなく安全性の話を伴う。

| ★ | 箇所 | 内容 |
|---|---|---|
| ★ | `saiverse/autonomy_wiring.py:277-291` | `_judgment_lock` が getattr→callable 検査→try→hasattr の 4 重防御の果てに `nullcontext()` を返す。**発火すると判断 Pulse の per-persona 直列化が黙って解除される**。`manager.meta_layer` は `saiverse_manager.py:245` で無条件設定、`MetaLayer._get_lock` は実質 raise しない。→ `manager.meta_layer.get_lock(persona_id)` の 1 行に |
| | `sai_memory/curation_ops.py:1155` | `adapter._db_lock or _threading.RLock()` — 直上で `adapter.conn` を確認済み、`_db_lock` は `adapter.py:156-157` で無条件存在のため**到達不能**。発火したら別ロックで同一 conn に交錯 = `db_locks.py` が 2026-08-06 に潰した事故の再発形態。→ 直接参照へ |
| | `saiverse/occupancy_manager.py:103-127` ほか | `get_region` の getattr 失敗時に**警告なしで入口トポロジー検査を放棄**(=全移動無制限通行)。管理者用 gate が閉じる側に倒れていない。→ fail-closed 化 |
| | `saiverse_memory/adapter.py` 約 35 箇所 | `except Exception → return []/0/None` の網羅 catch。`database is locked`(DB 障害)が「記憶ゼロ」として上流に消費される。実例: `get_embed_metadata` の失敗→None が「reembed 推奨」の誤状態化(`adapter.py:309-316`)。有三値(値/空/失敗)が一部に既に入っており(`count_pending→None` 等)、**流儀の普及ムラ**が実態 |
| | `llm_clients/nvidia_nim.py:318-331` | 生 HTTP 経路の全例外を `RuntimeError("NVIDIA NIM API call failed")` に平準化 → `openai_errors.should_retry` も空応答判定も種別を見失う。**既知 issue `llm_retry_stacking_stalls_jobs_silently.md`(fb214de7)が観測されたまさにこの経路**。→ httpx→LLMError 変換表で再 raise |
| | `persona/history_manager.py:117-118` | 旧ログ json の読込失敗を**無言で** `data=[]` にして上書き = 唯一の except→握りでデータ喪失経路。→ warning+退避 |
| ★ | `saiverse/dynamic_state.py:198` | `getattr(persona, "persona_dir", None)` — **PersonaCore に `persona_dir` は存在しない**(persona/core.py 実読で定義ゼロを確認済み)。`pdir` は常に None で `tools/context.py:225` に文字列 `"None"` が載る潜在バグ。実体は `sai_mem.persona_dir`。CLAUDE.md「default が返ったら名を疑え」の実演。→ `sai_mem.persona_dir` へ |

---

## 3. 横断パターン別カタログ

### A. 常設属性への getattr 防御(全リポジトリ最大クラスタ・約 200 箇所)

自分所有の `__init__` 無条件代入属性を getattr デフォルトで守っており、リネーム・タイプミスの瞬間に「None 扱いで黙って機能停止」に化ける。CLAUDE.md `Never guess attribute names` 節が警戒している隠蔽の量産品。

- `saiverse/` ≈94 箇所(例: `judgment_points.py:1641-1643` pulse_controller None→判断が黙って abort。`voice_call.py:276` 常設 2 属性の二重 getattr)。置換先は全て `saiverse_manager.py:168-277` / `manager/initialization.py:54` で無条件設定を確認済み
- `api/`+`manager/` ≈30 箇所(system/config/info/chat 各ルート)。`api/deps.py:get_manager` は未初期化時に例外を投げるため `if manager else None` 節が死枝(`arasuji.py:137,1120` 等)
- `sea/` ≈50 箇所(`head_pipeline/integration.py:1050-1051`、`pulse_controller.py:650-661`、`beat_gate.py:248` は docstring が「テスト SimpleNamespace との互換シーム」と自認 = テスト都合が本番経路に漏れている)
- 記憶: `saiverse_memory/adapter.py:2094-2107` — 全フィールド常備の `Message` dataclass へ getattr。同じ関数内で `msg.line_role` だけ直接アクセスという**地層のムラ**がレビュー積み増しの証拠
- ★ 空振り二重保険: `getattr(manager,"sea_runtime",None) or getattr(manager,"runtime",None)`(`sea/head_pipeline/integration.py:1050`、`saiverse/day_plan.py:2423`、`dynamic_state.py:414`、`api/routes/people/cache_status.py:141`)— 第 2 候補 `RuntimeService` には `session_lifecycle` が存在しない(grep 実測 0)ため、第 1 が失敗した世界で第 2 も必ず空振り。`sea_runtime` は常設。→ `manager.sea_runtime.session_lifecycle` 直参照に
- `sea/head_pipeline/integration.py:99-102` `resolve_default_model_key` — 第 2・3 候補の `default_model`/`DEFAULT_MODEL` は**実在しない属性**(`2026-07-17_audit_wave_session2_handoff.md` で実バグ確定済み)。「テストスタブ互換」で本番解決順に残すと同じ `MODEL_KEY='default'` 崩落を再現できる。→ `persona.model` 1 段に

**処方**: 本番コードは直接アクセスへ、欠けた对象はテスト fake 側で埋める。機械的・1 箇所ずつ安全。専用のリファクタコミットにして ruff 通過。

### B. リトライ/フォールバックの多重積み(未解決 issue `llm_retry_stacks_stalls_jobs_silently` と同根)

| 経路 | 実測の積み構造 |
|---|---|
| OpenAI | ★ 外側 `call_with_retry(max_retries=3)`(`openai_runtime.py:80-110`、呼び出し 4 箇所)× SDK リトライ(クライアント構築時 `max_retries` 未指定 `openai.py:319`、リクエスト側 `max_retries=3` を明示 `openai.py:494` ほか)= 最悪 12 試行の掛け算 |
| Anthropic | SDK 既定(2 リトライ、`anthropic.py:88-91` に `max_retries` なし)× 自前 `_execute_with_retry(3)`(`:256-297`)× streaming 用にリトライループ 2 番目のコピー(`:503-546`)= 最悪 9 試行。timeout 1800s と掛けると理論 4 時間超 |
| Gemini | SDK `HttpRetryOptions(attempts=5)`(`saiverse/gemini_clients.py:34-45`)× client ループ 3(`gemini.py:1504-1964`)× free→paid 切替。gemini.py の docstring 自身が「SDK が 429 を 5 回再試行して無駄な往復」と認めている |
| 空応答 | client 内側 continue 3 回(`gemini.py:1698-1875`)× 外側 `generate_text_with_empty_retry` 3 回(`sai_memory/arasuji/generator.py:93-144`)= 整理経路で最大 9 API 呼び出し。ollama 側は「外側リトライに依存」とコメントで明記しており**方針分断** |
| Ollama | endpoint 間フォールバック × endpoint 内リトライの多重連鎖(v1→/api/chat→legacy で最大 9 リクエスト)+ `self.chat_url` が `__init__:163` で常時設定のため発火不能ガード 5 箇所 |

**処方**: 「SDK 内リトライは 0/1 に固定し、戦略リトライは 1 層(client か caller か)にのみ置く」を原則化。`_call_with_client` 死コピー(`gemini.py:1372-1397`)とエラー分類 5 系統コピー(`openai_errors.py` ≈ `anthropic_retry_policy.py` の行単位重複ほか)も同時に対象。429 耐性の見かけ上の低下を伴うため backoff と同時裁定・要実機検証。

### C. 供給源が消えた互換層・no-op 残骸(挙動を変えず削れる純減分)

- `saiverse/` **台帳なし縮退 ≈400 行**: `day_plan.py:_fire_slot_legacy`、`occupancy_manager.py:_move_entity_legacy`(141 行)ほか 6 モジュール 9 箇所 — `execution_ledger` は `saiverse_manager.py:208` で無条件構築されるため**本番では絶対に入らない並行実装**。`tests/test_day_plan.py:2095` が专门テストでこの層を生かしけている(`test_day_plan.py:1526` で本物 Ledger を積めることは実証済み)。→ テストハーネスに実 Ledger を積んで撤去
- `manager/history.py`: no-op shim ×3(生きた呼出元 8 箇所と対で削除)+ `_legacy_backup/restore_world_disabled` の 1 行目 `raise` の後ろに ~115 行の到達不能コード
- `saiverse/conversation_manager.py` 一式: no-op 化は landscape §9 に削除 pending 明記済み。`start_autonomous_conversations` の実コール元ゼロ、`trigger_next_turn` ゼロ。実体化・stop ループ・常真ガード(`saiverse_manager.py:1593`)ごと
- 凍結 inter-city の残骸: `saiverse_manager.py:56-57` の死 import、`db_polling_stop_event`(読む側が死)、`manager/background.py` 繋がり(登録元ゼロ)
- `saiverse/autonomy_manager.py:46-257`: 休眠互換 API(`set_interval`/`set_models`/`get_status`)— 存在根拠の docstring に対し読む API ルートが存在しない(grep 実測)
- `llm_clients` の `generate_with_tool_detection` 互換チェーン 6 箇所 — 実呼出元はテストのみ
- `api/routes/chat.py` 単数形 `attachment` — frontend 全体で送信ゼロ(実測)。`api/routes/people/models.py:455-460` の deprecated 6 フィールド「受理して無視」配管 3 層も同様
- `GEMINI_SAFETY_CONFIG` 二重定義(llm_clients 側が供給源なし legacy、唯一の実使用は `llm_router.py` のローカルコピー)
- `saiverse_manager.py:437-442` の `item_registry`/`items`/`state.items` 同一 dict 別名 3 系統と、それに対する 3 段 hasattr はしご(`api/routes/info.py:409-414`)
- `sea/langgraph_runner.py` 救済チェーン(★): langgraph は `requirements.txt:32` のハード依存。docstring が約束する「lightweight runner」は**リポジトリに存在せず**、`compile_with_langgraph` は None を返さず raise するため `runtime_runner.py:202` の None 分岐も到達不能。内側 except 6 個も外側 except と同一末路。→ 素 import + 死分岐削除
- `_basic_chat_playbook` インメモリ最終フォールバック(`sea/runtime.py:2652-2682`): コメント自認「絶対に到達しないことを期待」。発火条件は「builtin 欠損+DB 欠損」の重畳で、その時 no-op playbook に逃れるのは誤った救済

### D. 起動ごとに積む層(起動原価と無管理ファイル)

- **毎起動フルバックアップ 2 回**(`main.py:344-347` pre_upgrade + `:465` startup)× コピー毎 `integrity_check` × prune 時に既存全件(keep10×kind2)再検証 = 最悪 ~24 回のフル DB 読破。manifest な旧世代 .bak は検証不能ゆえ**永久に削除されず堆積**(`database/backup.py:134-185`)
- keep/prune の 3 実装(`database/backup.py` / `sai_memory/backup.py` / `sai_memory/backup.py` rdiff 版)+ `migrate.py:326-354` の `.bak` はどの prune パターンにも match せず**無制限堆積**
- `sai_memory/backup.py:154-231`: グローバルロックが **fcntl 限定 = win32 では機構ごと不発**。stale-lock 機構 ~80 行が空振りし、Windows では多重バックアップがノーガード。「鍵があるつもり」で動いている
- 常設化マイグレーション 14 本の毎起動直列実行(`main.py:368-431`)。clips/desk の正規化は**毎回全行 SELECT+Python 行単位 UPDATE**。→ 完了マーカー(既存 embed_metadata 方式)で 1 回ゲート
- legacy embeddings テーブル: 供給源 INSERT が repo 内ゼロ(`upsert_embedding` は `message_embeddings` へ)なのに毎 init で CREATE+バックフィル
- SQLite busy リトライの 4 部コピー(`database/building_messages.py`)× 土台の `busy_timeout=5000` × manager 側エンジンの pragma 再実装(`manager/initialization.py:35-48`)の三重同居 → `_with_locked_retry` 1 本+エンジン統合

### E. 同型ボイラープレート(集約候補・レビューで増殖)

- `except Exception → HTTPException(500, f"…{e}")` 同型 **48 箇所**(密集: `memopedia.py` 17、`arasuji.py` 10)。`pocketbook.py:219-221` は専用枝と汎用枝が同一 500 の純粋な二重積み → ルータ単位 exception_handler へ
- manager の「open→try→commit/except rollback+`"Error: {exc}"`→close」同型 **53 箇所** → `with self.tx() as db:` 1 本(戻り値の `"Error:"` 文字列契約は `people/config.py:156` が依存しているので維持)
- `builtin_data/tools/` の `get_active_manager()→None なら raise` ボイラープレート **43 箇所 35 ファイル** → `require_manager()` 1 関数へ
- 2-copy 群: `/config` payload 組み立て(`config.py:346-375`≡`481-509`)、WSL パス復旧はしご(`info.py:443-509` と `629-644`、コメント自身が複製を申告)、memopedia LLM 初期化(`memopedia.py:595-618`≡`817-840`)、ジョブ台帳(`memopedia.py:540` ≡ `arasuji.py:28`)、`_resolve_persona_name`/`_resolve_budget` が同一リクエストで同じ AI 行にセッション 2 本(`core_memory.py:94-130`)
- `manager/runtime.py:165-167` `dispatch_timeout_seconds` — **設定する側が repo 内に存在しない**ノブ

### F. 二重化しているように見えて実は…(呼び出しチェーン実読で確定した重複)

- `sea/work_session.py:820-828`: anchor touch の caller 側 try/except — callee(`session_lifecycle.py:1539`)は全経路自己 catch 済みで、同じ関数を `runtime_llm.py:405` はラップなしで本番稼働中。相互矛盾の実証
- `sai_memory/memory/storage.py:1509-1539` + `adapter.py:1671-1680`: `delete_thread` の内側が全 except→False なので外側 except は発火不能。locked が DEBUG 1 行で消える
- `saiverse/execution_ledger_wiring.py:249-299`: step 関数が全例外を自己吸収するため tick 側の step 別 outer try は原則到達不能 → 入口 wrapper 1 枚に
- `autonomy_manager.py:317-341`+`pulse_dispatcher.py:290-298`+`event_scheduler.py:292-300`: 同一 Exception の 4 層 swallow。dispatcher の swallow が原因で `_handle_tick` の `report.status="error"` が watchdog 経路で**永遠に設定されない**(report 機構が生きていると思い込んでいる)

---

## 4. frontend(Backend とは堆積の質が違った)

仮説の多くは**外れ**(mountedRef 0 箇所、TanStack 未導入につき三層ガード不成立、SSR ガード 7 箇所のみ、エラー出口の二重表示なし — 7b659931 の出口一本化は機能している)。残った実態:

- `/api/config/developer-mode` を **5 コンポーネントが個別 fetch + CustomEvent で手動同期**。ArasujiViewer 側は取得値をレンダーで一度も読まない死んだ网络呼び出し(ESLint 確認) → 1 箇所の Context 化
- `page.tsx:505-521` `isMobile` 死んだ state+常駐 resize リスナ(読み手ゼロ、`:3279` のコメント自認)
- `resolveHasMore`(`page.tsx:809-811`): 後端が `response_model` で `has_more: bool` を必ずシリアライズするためフォールバック再導出が到達不能。型保証済み配列への `|| []`(約 10)/`Array.isArray`(約 6)も同族 — TS interface を response_model に合わせて 1 定義すれば型で消せる
- `ChatOptions.tsx:294-300`: AbortError 専用文言は `Promise.allSettled` が absorb するため**表示され得ない**。`:391` のコメント自認「applyModelChange は内部で全例外を握るが、万一漏れた場合の保険」= 二重ガードの自認書
- `VoiceCallModal.tsx`: 仕様上 throw し得ない 6 操作(`WebSocket.close`・`disconnect`・`stop` 等)への try/catch。対照的に `AudioContext.close().catch` は正当で残す
- `notices` 正規化+alert の逐語コピー ×5(ChatOptions/GlobalSettingsModal)→ `lib/notices.ts` 1 本
- ESLint no-unused-vars 40 件超 = 消した機能の残骸 state/job id 生成系の地層(`PersonaMenu.tsx:52` は応答を await して破棄、等)
- alert 型出口と setError 型出口の二世代混在(MemopediaViewer alert 16・MemoryBrowser 12・ArasujiViewer 11・CityMap 混在)。`WorldEditor.tsx` の `apiCall` は alert を隠匿しテスト不可 — 出口統一は UI 裁定事項
- 補足: frontend には ErrorBoundary が**一つもない**(重複の逆の欠如)。page.tsx が catch 56 を手動背負う一因

---

## 5. 検証済み・無罪(削ってはならないと実読で確認したもの)

過剰削減の防止策として、**レビューで正当と確認した境界防御**を明記する:

- LLM/SDK 応答への防御全般 — `gemini.py` の getattr 91 件の大半は SDK 応答オブジェクト向け。SSE 中間 504 monkey-patch、digit-loop 検知は実測障害への防御
- ペルソナ記憶の無垢系 — memopedia rollback スナップショット、後発編集ブロック、native_export の import 関門、台帳冪等の「握らず例外で表明」層(逆に戻してはいけない)
- 並行実体のあるロック — BeatGate/PulseController、reflex の thread+Event+二重 timeout(最小構成と評価)、WAL スナップショット 1 回リトライ、`db_locks.py` の錠前配り設計
- `uri.py:30` の getattr(遅延属性で実働)、CAS 競合の `.code`、位置競合の多重照合(責任分担コメントあり)、水位検証、journal 付き移行(`building_id_repair` 等=期限付き移行で本書対象外)
- `factory.py` の isinstance ゲート(config が外部 JSON 由来)、`_as_token_count` の値検査(provider 応答向け)
- frontend: 送信→ストリーム→出口の一回構造(reply_stop_exit 設計)、Tailscale secure context 対策の randomUUID フォールバック

**教訓の受け口**: 「発火不能ガード」の最大供給源は**テストスタブの簡素さ**(SimpleNamespace/MagicMock のため本番コードが属性を守らされている)。fake の質を上げれば本番コードから盾が構造的に減る — 本監査所見の A/C/D に共通する上流。

---

## 6. 着手原則(本書が全面一掃の命令でないこと)

CLAUDE.md の scope discipline(触る予定のない場所のリファクタは回収不能)に従い、**本書は候補台帳**。想定順序:

1. **即効・低リスク**(挙動不変の機械削除): §2 の ★3 件(`persona_dir`・`_judgment_lock`・`curation_ops`)、langgraph 救済チェーン、`manager/history.py` no-op 群、is_proxy ガード 11 箇所、互換層純減(§C の llm/tools 側)、frontend P1-P3、ESLint 残骸
2. **未解決 issue と統合**: §B は `llm_retry_stacking_stalls_jobs_silently.md` の解決設計に繰り込み(429 耐性の変化を含むためまはー裁定+実機検証必須)
3. **規律の横展開**(触った区画から): getattr 直接化(A)、有三値の読み口横展開(記憶 P3)、ボイラープレート集約(E)。専用のリファクタコミットに分ける
4. **裁定が必要**: 台帳縮退撤去(テストハーネス方針)、バックアップ 1 本化+Windows ロック実装載せ替え、fail-open→fail-closed 化(§2 の occupancy)、WSL パスしご撤去(生産 DB 読み取りクエリ 1 本が前提)、alert 出口統一(UI 判断)

### 要追加検証(本書で断定を保留したもの)

- `items.FILE_PATH` の旧 WSL 形式残存有無(読み取り 1 クエリ)
- 単数形 attachment / deprecated フィールドの `git log -S` による供給元消滅時期(リポジトリ外自作クライアントの記憶)
- 500 detail 文字列へのフロント分岐依存 grep(HTTP 集約時)
- `api/routes/people/recall.py:336` と `adapter.recall_hybrid` の RRF 二重実装の可能性
- `migrate.py:596-710` の v0.2 世代 DB 供給絶滅の有無(配布統計)
- `sea/reply_stop_exit.py:125-148` の `update_building_message` 返値契約(`persona/history_manager.py` 実読)
- memopedia vividness / keywords shim の旧行残存(実 DB)
- `openai_codex_auth.py` lock の総時間上限、`mcp_client.py` と MCP SDK 内蔵再接続の二重化

### 検証の限界の宣言

本書の所見は並列エージェントが callee/caller を実読した結果であり、看板 5 件(★)は私が直接再検証した。**残りは実装前に必ず現場の行を再読すること**(audit_20260730 の教訓 — 監査結果自体も独立検証の対象)。行番号は 2026-09-26 時点の develop(`dea78163`)。
