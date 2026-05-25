# Intent Document: Cache Lifecycle Control

**ステータス**: Phase 1-2 実装済み (v0.5, 2026-05-24)
**位置付け**: prompt cache の **lifecycle 制御層**。`cached_head_architecture.md` が「head 内容の安定性」を保証するのに対し、本機構はその安定した head に対して「キャッシュを **いつ書き** / **いつまで保持** / **いつ捨てる**」かを制御する。Anthropic / Gemini 両プロバイダの cache 機構を統一 UI で扱う。
**前提**: `cached_head_architecture.md` (head 安定性が成立している前提で TTL 戦略を組む。**実装済み**: `sea/head_pipeline/` — registry/store/pipeline + sections 11 種) / Gemini explicit cache の実測結果 (`temp/verify_gemini_cache_billing.py`、書き込み無料 + storage 課金 + extend 可能)

---

## 1. これは何か / 何でないか

### これは何か

LLM provider の prompt cache を、3 つの利用モード (**標準 / 連続 / マニュアル**) で抽象化し、chat session ごとに切り替え可能にする仕組み。各モードは TTL 戦略の違いを表現する:

- **標準モード**: 短 TTL。単発会話想定、storage 課金リスクを抑える安全装置として動く
- **連続モード**: 長 TTL。継続的な対話想定、TTL 切れによる cache miss を避ける
- **マニュアルモード**: TTL を会話開始前に手動指定。見込みが明確な時に使う

加えて、現在生きているキャッシュが**効いているか / 残り時間**をリアルタイムに可視化し、手動 extend / delete を提供する **キャッシュタイマー UI** を ChatOptions モーダル付近に常設する。

タイマーに載せるのは **即時性のある情報のみ** (効いてるか / 残り秒)。累計ヒット token / 推定節約額のような**即時性のない集計値はタイマーに載せない** — それは Usage ページ (`/api/usage/*` + 既存 UI) の領分とする (理由は §1「これは何でないか」)。

### これは何でないか

- head 内容の安定性を保証する仕組みではない。それは `cached_head_architecture.md` の責務
- ペルソナの autonomous pulse (自律行動) における cache 戦略は scope 外。本機構は **chat UI からの対話** における cache 制御を扱う (自律行動連携は別 intent doc で後追い、§8 に展望のみ)
- 累計集計 (累計ヒット token / 節約額) の表示機構ではない。これらは即時性がなく、タイマー (1-2 秒粒度の即時表示) に載せる性質ではない → **Usage ページの領分** (`/api/usage/*` に集計 API + UI が既存)。タイマーは「効いてるか / 残り時間」だけを扱う
- provider 横断の billing 集計ダッシュボードではない
- cache 設定の永続化機構ではない。設定はセッション (chat tab) 寿命中のみ有効、再オープンでデフォルト (= モード = 標準) に戻る

### なぜ必要か

#### Anthropic 既存実装の限界

ChatOptions には既に `cache_enabled` トグル + `cache_ttl` 選択 (5m / 1h) が実装済み (`frontend/src/components/ChatOptions.tsx` L53-)。ただしこれは「raw TTL を直接ユーザーに見せる」UI で、以下の課題がある:

1. **意図と設定の距離**: ユーザーは「単発か継続か」を考えているのに、TTL の数値を選ばされる。意図と設定の翻訳が UI を使う人に押し付けられている
2. **キャッシュの現状が見えない**: 現在キャッシュが効いているか、残り時間がいくつか、累計でどのくらいヒットしたかが分からない。設定はできるが結果が見えない
3. **Gemini 未対応**: 現状 Anthropic のみ。Gemini 用 explicit cache を追加すると、provider ごとに別 UI が増えて一貫性が崩れる

#### Gemini explicit cache の特性

実測で確認した通り (2026-05-22):
- 書き込み課金は**ない** (create 自体は無料)
- 維持コストは storage = $1.00 / 1M tokens / hour
- cached hit は通常 input の 1/10
- → **breakeven = 1.35h 以内に 1 回ヒットで黒字** (Gemini 3.5 Flash)
- TTL は `caches.update` で extend 可能、コンテンツ更新は不可
- `caches.list` で orphan 検出可能、`caches.delete` で明示削除可能

暗黙キャッシュは TTL が短く (実測 5 分未満) 信用できないため、長時間対話では explicit cache を能動的に張る必要がある。

#### Anthropic cache の特性 (対比)

Anthropic は課金構造が Gemini と逆である:
- **書き込み時に課金** (cache write は通常 input より割高な一度きりのコスト)
- 保持中の **storage 課金はない** (張りっぱなしでも追加コストゼロ)
- cached hit は通常 input より大幅に安い
- TTL 内に同じ prefix を再送すると cache hit (安価) のまま **TTL がリセットされる = 実質「無料 extend」**。新規 write 課金を再度払わずに寿命を延ばせる

#### provider で最適戦略が逆転する (本機構の核心)

同じ「prompt cache」でも、コストを最小化する運用は provider で正反対になる:

| | Anthropic | Gemini |
|---|---|---|
| 保持コスト | なし | storage 課金 (時間比例) |
| 無料 extend | あり (再送で TTL リセット) | なし (`update` も時間課金が続く) |
| 最適戦略 | **延命が得**。安い再発話で cache を温め続け、idle 保持も無害 | **idle 保持が損**。生きてる間に集中実行し、用が済んだら即 delete |

この逆転を理解した上で 3 モードを読むと意味が通る:
- **標準モード** (pulse 終了 delete) は Gemini の storage 出血を即止める意味で Gemini 向きの安全側デフォルト。Anthropic では delete が no-op だが storage 課金が無いので害もない
- **連続モード** (pulse 跨ぎ保持) は Anthropic では純粋に得 (保持無料 + 無料 extend)。Gemini では「確実に話し続ける」前提でのみ黒字 = §3.2 の breakeven 1.35h が守るライン

#### 統一抽象の必要性

Anthropic implicit auto-cache (cache_control 付与で API 側が自動キャッシュ) と Gemini explicit cache (create/delete を明示) は挙動が大きく異なるが、ユーザーから見ると「会話の cache をどう運用するか」の問題は同じ。3 モードによる抽象化で、provider 切り替え時にも同じ操作感で扱えるようにする。

---

## 2. 設計原則

### P1. モードは「意図の表明」、TTL は「モードの結果」

ユーザーが選ぶのはモード (標準 / 連続 / マニュアル) であり、TTL は各モードの戦略に従って自動決定される (マニュアルのみ TTL 直接指定)。これにより「意図の表明」と「実装の判断」を分離する。

### P2. provider 差異は内部で吸収、UI は統一

Anthropic / Gemini の挙動差 (delete API の有無、storage 課金の有無、extend 手段) は backend で吸収。UI は「現在のキャッシュ状態」と「モード選択 / 手動操作」を provider 非依存に提示する。provider 固有の制約 (例: Anthropic は delete 不可) は対応ボタンを disable + tooltip 説明で表現する。

### P3. キャッシュ可視性はリアルタイム

「現在キャッシュが効いているか」「残り何分か」を 1-2 秒粒度で表示する。設定だけ提供して結果が見えない既存 UI の欠陥を是正する。累計節約額のような非即時情報は対象外 (Usage ページ送り)。

### P4. セッション設定 (永続化しない)

cache モードは chat tab 寿命中のみ有効。tab を閉じて開き直したらデフォルト (= 標準モード) に戻る。
理由: cache 戦略は「これから何をするか」の宣言であり、過去のセッションを引き継ぐ性質ではない。永続化するとモード設定の老朽化 (= 過去の会話用に張った設定が新会話で意図せず効く) が起きる。

### P5. orphan cache は起動時に掃除

異常終了で残った Gemini explicit cache は、起動時に `caches.list` → `displayName` prefix で SAIVerse 由来を識別 → delete する。永続的な storage 課金リークを防ぐ安全装置。

### P6. 最適戦略は provider 依存、UI は統一

§1 の通り Anthropic は延命が得・Gemini は即 delete が得と最適点が逆転する。この差は backend の TTL 戦略と「どのモードが各 provider に向くか」に反映するが、UI の操作体系 (モード選択 + タイマー + extend/delete) は provider 非依存に統一する。ユーザーに provider ごとの課金モデルを学習させない (学習コスト削減) のが統一の目的であり、最適化は backend が引き受ける。

---

## 3. 3 モード仕様

### 単位の定義

「**pulse**」= 1 playbook 実行 (`sea/runtime_runner.py:_run_playbook` 1 回、`pulse_id` 1 つ)。chat UI からのユーザー 1 メッセージ送信 = meta_user playbook 起動 = 1 pulse。1 pulse 内で複数の LLM コール (router / think / speak 等) が走る。

cache の生存単位はこの pulse、もしくは pulse を跨いだ持続。

### 3.1. 標準モード (default)

| 項目 | 値 |
|---|---|
| Anthropic TTL | 5 分 (デフォルト) |
| Gemini TTL | 15 分 |
| 用途 | 単発会話 / 短時間の質疑 / 試しに話してみる |
| 振る舞い | **pulse 開始で cache 作成、pulse 終了で明示 delete**。TTL は異常終了時 (delete 失敗) の保険として効く |

1 pulse 内の複数 LLM コールで cache hit を得るのが標準モードの主目的。pulse が終われば次の pulse で同じコンテキストを再利用する見込みがないので即座に捨てる。TTL 15 分はあくまで「delete が走らなかった時に orphan を残さない」ための保険であり、正常時の cache 寿命とは別概念。

### 3.2. 連続モード

| 項目 | 値 |
|---|---|
| Anthropic TTL | 1 時間 (`ttl="1h"`) |
| Gemini TTL | 1.35 時間 (= breakeven 直上) |
| 用途 | 同じペルソナと連続して話す / 継続的な議論 |
| 振る舞い | pulse 開始で cache 作成 (既に生きてれば再利用)。pulse 終了でも **delete しない**。TTL 経過で自然消滅。ユーザーが明示的に delete / extend ボタンを押せば実行 |

pulse を跨いで cache を保持することで、連続発話の 2 回目以降を cached hit にする。自動 extend は行わない (まはー指示: 「extend は救済用、delete と同列の手動制御」)。新ターンで cache 内容を更新したい場合は、ユーザーが手動 delete してから次 pulse で新規書き込み、という運用。

Gemini の TTL = 1.35h は breakeven 計算の直上で「1 回でも追加発話があれば必ず黒字」を保証する値。

### 3.3. マニュアルモード

| 項目 | 値 |
|---|---|
| Anthropic TTL | ユーザー指定 (5m / 1h から選択) |
| Gemini TTL | ユーザー指定 (5m / 15m / 30m / 1h / 1h20m から選択) |
| 用途 | 上級者向け / 会話時間の見込みが明確な場合 |
| 振る舞い | 連続モードと同様 (pulse 跨ぎ保持 + 自然消滅 + 手動制御) だが TTL のみ手動指定 |

既存の `cache_enabled` + `cache_ttl` UI はマニュアルモードの実体として残す (= 既存実装の上に「標準 / 連続」プリセットを追加する形)。

### 3.4. モード切替

ChatOptions モーダル内に「キャッシュモード」セクションを追加、ラジオボタン or セグメントコントロールで標準 / 連続 / マニュアルを選択。マニュアル選択時のみ TTL 選択 UI が露出。

---

## 4. アーキテクチャ

### 4.1. レイヤー構成

```
[Frontend: ChatOptions]
  ├─ モード選択 (radio: 標準 / 連続 / マニュアル)
  ├─ TTL 選択 (マニュアル時のみ)
  └─ キャッシュタイマー UI (常設)
      ├─ 現在のキャッシュ状態 (provider / 効いてるか / TTL 残り) ※累計/節約額は載せない (Usage ページ)
      ├─ extend ボタン (provider 機能に応じて enable/disable)
      └─ delete ボタン (provider 機能に応じて enable/disable)
        ↓ GET /api/people/{persona_id}/cache-status (polling) + extend/delete endpoint
[Backend: api/routes/chat.py + 新規 cache lifecycle controller]
  ├─ ChatSession ごとに CacheLifecycleState を保持 (in-memory)
  ├─ モード → TTL 翻訳
  ├─ provider 抽象 (ICacheController)
  └─ orphan cleanup hook (startup)
        ↓
[LLM clients]
  ├─ anthropic.py: cache_control (`{"type":"ephemeral","ttl":"5m"|"1h"}`) の付与
  └─ gemini.py (新規): caches.create / update / delete / list
```

### 4.2. cache の key 単位

cache は `(persona_id, line_id)` 単位で持つ。これは `cached_head_architecture.md` の `LineHeadSnapshot` と同じ key 粒度で、head 安定性と lifecycle 制御が同じ単位で動く。

**実装確認 (2026-05-24)**: DB テーブル `line_head_snapshot` (`database/models.py`) の PK は `(PERSONA_ID, LINE_ID)`、`MODEL_KEY` は PK 外の値カラム。`store.py` の load/save/delete は全て `filter_by(PERSONA_ID, LINE_ID)` で動く。本機構の key 粒度はこの実装済み store とそのまま揃う。

- メインライン: `(persona_id, "main")` で 1 本
- サブライン: `(persona_id, 起点識別子)` で複数並走しうる
- 入れ子ライン: 親 line_id を継承 (sub-playbook が親 `_pulse_id` を継承するのと同じ構造)

**現状 (Phase 進行度)**: `sea/head_pipeline/integration.py` で `line_id` は **`"main"` 固定**。サブライン / 入れ子ラインは未配線 (cached_head 側の Phase 3+ 待ち)。よって本機構の Phase 1-3 もメインライン 1 本のみが対象 (§8.2 と整合)。`model_key` は persona の `DEFAULT_MODEL` を採用し、1 ライン 1 モデルが常に成立する (モデル切替は `model_changed` イベントで snapshot 再 capture)。このため provider/model は key ではなく `CacheLifecycleState` の属性で持てば足りる (§4.3)。

### 4.3. CacheLifecycleState

**導入時期: Phase 2**。Phase 1 (タイマー = 残り時間のみ) は anchor から read-only で算出するため state を持たない (§7 Phase 1)。CacheLifecycleState が要るのは「モード」を保持する Phase 2 から。

```python
@dataclass
class CacheLifecycleState:
    persona_id: str
    line_id: str
    mode: Literal["standard", "continuous", "manual"]
    manual_ttl_seconds: int | None  # mode=manual 時のみ
    provider: Literal["anthropic", "gemini"] | None
    cache_handle: str | None        # Gemini: cache.name / Anthropic: marker
    created_at: float | None         # anchor.updated_at に揃える (§5.2)
    expires_at: float | None         # created_at + resolve_ttl_seconds(state)
```

累計ヒット / 節約額のフィールドは持たない (タイマーに載せず Usage ページの領分 — §1)。`last_extended_at` も持たない (自動 extend がないため)。手動 extend / delete はユーザー操作で都度発火、状態は `expires_at` の更新だけで表現する。

**TTL は state に数値として持たない**。`mode` (+ `provider`、manual 時のみ `manual_ttl_seconds`) から翻訳関数 `resolve_ttl_seconds(state)` で都度算出する (設計原則 P1「TTL はモードの結果」)。これが doc §7 Phase 2 の「モード → TTL 翻訳ロジック」の実体:

```python
def resolve_ttl_seconds(state: CacheLifecycleState) -> int:
    if state.mode == "manual":
        return state.manual_ttl_seconds
    return MODE_TTL_TABLE[state.provider][state.mode]  # §3 の表 (standard/continuous × anthropic/gemini)
```

### 4.4. pulse hook

cache lifecycle controller は `_run_playbook` の入口 / 出口に hook を持つ:

- **pulse 開始 hook**: `(persona_id, line_id, mode)` を見て、cache が無ければ create、有れば再利用 (既存 cache_handle を返す)
- **pulse 終了 hook**: `mode == "standard"` の時のみ delete。連続 / マニュアルは何もしない (TTL 任せ)

異常終了 (pulse 中の例外、プロセスクラッシュ) で pulse 終了 hook が走らなかった場合、cache は TTL まで残る → 起動時の orphan cleanup (§4.7) で拾う。

### 4.5. ICacheController (provider 抽象)

```python
class ICacheController(Protocol):
    def supports_explicit_create(self) -> bool: ...   # Gemini: True, Anthropic: False
    def supports_explicit_delete(self) -> bool: ...   # 同上
    def supports_extend(self) -> bool: ...            # 同上

    def write_cache(self, state: CacheLifecycleState, content: Any, ttl: int) -> str: ...
    def extend(self, state: CacheLifecycleState, new_ttl: int) -> None: ...
    def delete(self, state: CacheLifecycleState) -> None: ...
    def list_orphans(self, display_name_prefix: str) -> list[str]: ...
```

Anthropic 実装は `write_cache` で cache_control (`ttl="5m"|"1h"`) を付与するだけ (実体は LLM コール時に自動キャッシュ。実装箇所: `llm_clients/anthropic_request_builder.py` の `_make_cache_control()`、system / 末尾 message / tools の各末尾ブロックに付与)、`extend` は no-op (次回コールで cache_control 付け直しが §1 の無料 extend 相当)、`delete` も no-op (TTL 待ち + storage 課金なし)。標準モードの pulse 終了 delete は Anthropic では機能しないが、storage 課金が無く TTL = 5 分のため害はない。

Gemini 実装は API 呼び出しで全機能を実装。標準モードの pulse 終了 delete はここで効く (storage 課金を即座に止める)。

### 4.6. キャッシュタイマー UI のリアルタイム経路

選択肢検討:
- **polling**: `GET /api/people/{persona_id}/cache-status` を 2 秒間隔 (既存 people router に合わせる、実 prefix は実装時に確認)。実装単純、既存 polling 機構 (`useActivityTracker`) と整合
- **WebSocket**: 既存に WS 経路がないので新規インフラ整備が必要、コスト高

→ **polling 採用**。表示するのは「効いてるか + TTL 残り (整数秒)」のみなので 2 秒粒度で十分。pulse 完了 / 手動操作直後の状態更新は次の polling で拾える。

### 4.7. orphan cleanup

startup 時に各 provider の `list_orphans("saiverse:")` を呼び、見つかった cache を全 delete。`displayName` 規約は `saiverse:{persona_id}:{line_id}:{created_at_epoch}`。

これは標準モードの pulse 終了 delete が異常終了で走らなかったケース、および連続/マニュアルモードで TTL 未満にプロセスが落ちたケースの両方を救う。

---

## 5. 既存実装との関係

### 5.1. 既存 cache_enabled / cache_ttl UI

ChatOptions の既存 cache toggle (`cacheConfig.enabled` / `cacheConfig.ttl`) は **マニュアルモードの実装**として残す。標準 / 連続モードは内部でこの state を自動設定する形。

backend の `/api/config/cache` endpoint と model JSON の cache 設定欄も維持。新仕組みはこの上に「モード → TTL 翻訳ロジック」と「lifecycle controller」を追加する。

### 5.2. cached_head_architecture との接続

本機構は head が安定していることが前提。head が cache 安定帯にいる間だけ TTL を張る意味がある。Metabolism (= head 再構築) 発生時は cache を一旦 invalidate して再書き込みする必要がある。

→ Metabolism dispatch hook で `CacheLifecycleState.delete()` を呼ぶ。次回 LLM コール時に新 head で再 create。

**anchor (per-model) と snapshot/CacheLifecycleState (per-line) の粒度差**: `AI.METABOLISM_ANCHORS` は `{model_key: {anchor_id, updated_at, ttl_seconds}}` の **per-model** 構造。一方 head snapshot と本機構の state は **per-line** (`(persona_id, line_id)`)。1 ライン 1 モデルで両者は両立する。`METABOLISM_ANCHORS[active_model].updated_at` は **prompt cache 書き込みの真の起点** (= 最後に anchor を touch した時刻) であり、`integration.py:_resolve_anchor_ttl_state` が既にこれを読んで head pipeline の TTL 判定に使っている。本機構の `created_at` / `expires_at` もこの anchor.updated_at を起点に揃えるべき (独自の時刻起点を持たない)。

**書き込み時 TTL の記録と「短縮されない」規則 (重要)**: anchor entry には `updated_at` だけでなく **キャッシュの実効 TTL = `ttl_seconds`** も記録する (`_update_anchor_for_model` / `_touch_anchor_after_llm_call`)。これがないと既存キャッシュの残り寿命を**現行設定**で再計算してしまい、5m/1h 切替のたびにタイマー・head 再capture・metabolism anchor 判定が遡及的にズレる (2026-05-25 実機で顕在化)。

`ttl_seconds` の更新規則は **Anthropic の実測挙動**に従う:
- **生きているキャッシュは短い TTL で書いても短縮されない** (2026-05-25 実測: 1h 書き込み → 5m 書き込み → 5分超経過 → 5m 書き込みでもヒット)。公式 docs はこのケースを明文化していないが、「cache は使用のたびにリフレッシュ」とは明記。
- 書き込み時の更新規則 (モデルB、生存中の場合):
  - `ttl_seconds = max(既存, 新)` で**短縮しない**
  - **短い書き込み (新 < 既存) は expiry ウィンドウ (`updated_at`) をスライドさせない** — 起点を維持する。1h を 5m 書き込みで延命できると過大表示になるため (1h は「1h を確立した時刻」から減り続ける)
  - **同じか長い書き込み (新 ≥ 既存) のときだけ `updated_at` を now にリフレッシュ** (= 使用でウィンドウが延びる、自律 keep-awake の前提)
  - 完全失効後の書き込みは新 ttl / now でリセット
- 読み手 (`_anchor_entry_ttl_seconds` / `_resolve_anchor_ttl_state` / cache-status endpoint) は記録された `ttl_seconds` を優先、無い旧 anchor のみ現行設定にフォールバック。

**原則**: 「既存キャッシュの残り寿命 = 確立時の最大 TTL から計測 (短縮不可・短い書き込みでは延びない)」「次の書き込みに使う TTL = 現行設定」を分離する。タイマーは over-promise より **under-promise** を優先 (生きてると誤表示してミスする方が、切れたと誤表示するより害が大きい) ため、短い書き込みでウィンドウを延ばさない保守的なモデルを採る。

### 5.3. model_configs.py / claude-*.json

既存の cache 設定欄は維持。Gemini 対応のため `gemini-*.json` にも cache 設定欄 (`cache_type: "gemini_explicit"`, `ttl_options: [...]`) を追加。

### 5.4. 既存 TTL 配線の付け替え (Phase 2 の実体)

Phase 2 の「モード → TTL 翻訳」は **greenfield の新規追加ではなく、既存の global な TTL source を per-line state に格上げして再配線する作業**である。これを認識しておかないと「新しい TTL state を足したのに既存挙動が変わらない」という事態になる。

**現状の TTL source は単一の global 値** `manager.state.cache_ttl` ("5m"/"1h" の 2 択、city インスタンス単位で全ペルソナ・全会話が共有)。これを `_get_anchor_validity_seconds(model_key)` (`sea/runtime.py`) が秒数化し、以下 **3 つの消費者**が同時に読んでいる:

| 消費者 | 役割 | 経路 |
|---|---|---|
| a. Anthropic cache_control の `ttl` | 実発話の cache 書き込み TTL | `_get_cache_kwargs` → `cache_ttl` パラメータ |
| b. head snapshot 再 capture 判定 | anchor TTL 超過で head を作り直す (cache hit を諦め最新反映) | `integration.py:_is_anchor_ttl_expired` |
| c. Phase 4-e keep-awake pulse | TTL 接近で前倒し発話を予約 (自律側、§8.4) | `_schedule_cache_ttl_pulse` の `ttl_seconds` |

**付け替え方針**: `_get_anchor_validity_seconds` が `manager.state.cache_ttl` を直読みしている箇所を、「該当ラインの `CacheLifecycleState` を取得 → `resolve_ttl_seconds()` で秒数化」に差し替える。これだけで a/b/c の 3 消費者すべてが per-line のモード由来 TTL を読むようになる (= 1 箇所の付け替えで波及する設計)。`_touch_anchor_after_llm_call` / anchor.updated_at の解釈 (= cache 書き込み起点) は不変、TTL の**出どころだけ**を変える。

**重要な区別 (a vs b/c)**: 消費者 a (cache_control の ttl) は「**これから焼く**キャッシュの TTL」なので**現行設定**を使う。消費者 b/c (head 再capture / keep-awake) と timer は「**既に焼いた**キャッシュの残り寿命」なので、書き込み時に anchor へ記録した `ttl_seconds` を使う (§5.2)。`_get_anchor_validity_seconds` (= 現行設定) を既存キャッシュの寿命評価に流用すると遡及ズレが起きる。Phase 2 実装では b/c/timer を `_anchor_entry_ttl_seconds` (記録値優先 + 旧 anchor フォールバック) 経由に変更済み。

---

## 6. 不変条件

### C1. モード設定は session 寿命中のみ有効

cache モードは永続化しない (P4)。`CacheLifecycleState` は in-memory only、DB に書かない。

### C2. cache 寿命はモードで決まる

- **標準モード**: pulse 終了 hook で明示 delete (Gemini)。Anthropic は no-op (TTL 任せ)
- **連続 / マニュアルモード**: pulse 終了で何もしない、TTL 経過で自然消滅、ユーザー手動 delete も可

cache を勝手に消したり延ばしたりする自動機構は持たない。「TTL を最初に決めたらそれを守る」がコア原則。

### C3. provider 機能差は disable + tooltip で表現

UI は機能を「ある / ない」で隠さず、disable 状態で見せて tooltip で理由を説明する。これにより「Anthropic だと delete ボタンが消えた」のような驚きをなくす。

### C4. Metabolism 発生時は cache を invalidate

head が変わると cache hit しなくなるため、Metabolism dispatch で必ず lifecycle controller に delete を通知する。これは「自動延命」ではなく「無駄な storage 課金を切る」操作なので C2 の例外として扱う。

### C5. orphan cleanup は起動時必須

`displayName` prefix `saiverse:` で識別された Gemini cache は起動時に全 delete。これを skip すると storage 課金リークが永続化する。

### C6. 自動 extend は持たない

連続モードでも extend は手動操作のみ。「TTL 残り N 分で自動延命」のようなロジックは入れない。理由: モードを選んだ時点で TTL は決まっており、それ以上延ばしたければユーザーが意図的に extend ボタンを押すべき。

---

## 7. 実装方針 (Phase 分け)

### Phase 1: タイマー UI (Anthropic 限定、read-only)

- **`GET /api/people/{persona_id}/cache-status` endpoint** (read-only)。既存データのみから算出: `METABOLISM_ANCHORS[model].updated_at` (cache 書き込み起点) + `_get_anchor_validity_seconds(model)` (TTL) → 効いてるか / 残り秒。`get_cache_config(model)` で Anthropic explicit のみ `supported=true`
- **ChatOptions モーダル内に「キャッシュタイマー」セクション** (効いてるか + 残り時間バーのみ)、`useActivityTracker` パターンで 2 秒 polling
- 既存 `cache_enabled` + `cache_ttl` UI はそのまま残し、タイマー UI を併設

**state を持たない**。タイマーは anchor から read-only で算出するため `CacheLifecycleState` は導入しない (Phase 2 へ)。**累計ヒット / 節約額はタイマーに載せない** (Usage ページの領分、§1)。extend / delete ボタンも Phase 1 では出さない (Anthropic は extend/delete が no-op のため見せても意味がない。Gemini で意味を持つ Phase 3 で追加)。

この時点ではモード抽象は導入しない (= 既存マニュアル UI のみ)。タイマー UI が単独で動くことを Anthropic で検証。

### Phase 2: per-persona TTL 基盤 + 配線付け替え (✅ 実装済み 2026-05-24)

**スコープ判断 (まはー)**: モード UI は Anthropic 単独だと「標準=5m / 連続=1h の言い換え」に潰れ (§1 の通り pulse 終了 delete が no-op)、behavioral 差が出るのは Gemini の Phase 3。よって **Phase 2 は基盤 (per-persona TTL + 配線付け替え) のみ**とし、3 モード UI と `CacheLifecycleState` は Phase 3 に回す。

実装済み:
- **per-persona TTL override** を `manager._persona_cache_ttl: Dict[persona_id, "5m"|"1h"]` (in-memory・非永続) で保持。`get_persona_cache_ttl` / `set_persona_cache_ttl`。未設定 persona は global `manager.state.cache_ttl` にフォールバック
- **§5.4 の配線付け替え**: `SEARuntime._resolve_cache_ttl_str(persona_id)` を単一解決点に新設。3 消費者 (a: `_get_cache_kwargs(persona_id)` / b: `_get_anchor_validity_seconds(model_key, persona_id)` / c: Phase 4-e `_schedule_cache_ttl_pulse`) が全てこれを読むよう変更。`persona_id=None` は従来 global 挙動 (後方互換)
- 全呼び出し元 (runtime_llm.py 7 箇所 / `_resolve_metabolism_anchor` / integration.py / cache_status.py) に persona_id を伝播
- **UI (per-persona 1 箇所に統合)**: cache 設定の口を1つにする。タイマーセクションに「キャッシュ: オフ / 5分 / 1時間」セレクタ (実効値を表示、`POST /api/people/{persona_id}/cache-config` with `{setting}`)。`off`=enabled false、`5m`/`1h`=enabled true + TTL。これで「このペルソナとは連続対話するので 1h」を persona 単位で選べる
- 「データ送信量の管理」内に**重複していた** global cache UI (enabled/ttl チェック) は**削除**、「グローバル既定」表記も廃止 (まはー指摘: TTL を決める口が 2 箇所あって混乱。コードが動く ≠ UI が分かる)
- global `manager.state` (cache_enabled/cache_ttl) は UI 非表示の**初期既定**としてのみ残る。`/api/config/cache` endpoint と save-from-chat (model JSON へのキャッシュ既定保存) は温存
- 解決ロジックは `manager.resolve_persona_cache(persona_id) -> (enabled, ttl)` に集約 (per-persona override → global)。runtime / cache-status endpoint が共にこれを参照
- 注: `line_id` は "main" 固定のため現状は実質 per-persona。per-line は Phase 3+ (cached_head の line 配線と合流)

### Phase 3: Gemini explicit cache (実機検証必須なので M1-M4 に分割)

実機検証が必要なため小刻みに進める。Gemini の TTL 選択肢は Anthropic より細かく **オフ/5分/15分/30分/1時間** (storage 課金が時間比例なので中間値が有用)。

- **M1 (✅ 実装済み 2026-05-25)**: 「Gemini で実際に cache hit する最小配線」
  - `llm_clients/gemini_cache.py`: `GeminiCacheController` (create/reuse、`(model, sha256(system_instruction))` キー、最小トークンガード、in-memory)
  - `gemini.py`: `generate`/`generate_stream` が `enable_cache`/`cache_ttl` を受け、explicit cache が張れたら `cached_content` を渡し **system_instruction を除去** (二重送信しない)。キャッシュ対象は head (= system_instruction)
  - `gemini-2.5-flash.json` / `gemini-3.5-flash-paid.json` に cache 設定 (`type: "gemini_explicit"`, ttl_options, min_tokens=1024)
  - **安全ゲート**: env `SAIVERSE_GEMINI_EXPLICIT_CACHE` で全体 opt-in (cleanup/pulse-delete 未実装のため。M2-M4 後に撤去)。検証は log の `cached_content_token_count > 0`
- **M2**: head 変更 (metabolism) での明示 invalidate + 起動時 orphan cleanup (`caches.list` → `displayName` prefix `saiverse:` で delete)
- **M3**: タイマー UI を Gemini 対応 (cache resource の `expire_time` で残り表示)、persona/line 紐付け、Gemini の細かい TTL 選択肢を UI に
- **M4**: 標準モードの pulse 終了 delete (Phase 4 hook) + `CacheLifecycleState` / 3 モード抽象 + `ICacheController` 形式化 + UI の disable + tooltip

### Phase 4: pulse hook / Metabolism 統合

- `_run_playbook` 入口 / 出口に lifecycle controller の hook を組み込み
- 標準モードの pulse 終了 delete を実装
- Metabolism dispatch hook に invalidate 通知
- 手動 extend / delete ボタンの backend endpoint

各 Phase は単独でテスト可能。Phase 1 完了時点で「Anthropic 用キャッシュタイマー UI」として既に動く状態を作る。

---

## 8. 未確定事項 / 検討事項

### 8.1. 累計節約額は Usage ページの領分 (タイマーに載せない)

当初タイマーに「累計ヒット / 節約額」を載せる設計だったが、これらは**即時性がない** (1-2 秒粒度で更新する意味がない) ため、タイマー (即時表示) ではなく **Usage ページ**で見せる。`LLMUsageLog` (`PERSONA_ID / MODEL_ID / CACHED_TOKENS / COST_USD`) に既にデータがあり、`/api/usage/*` に集計 API も既存。節約額計算 (`cached hit token × (通常単価 - cache 単価)`) は `saiverse/model_configs.py:calculate_cost` / `get_model_pricing` を再利用できる。
→ タイマーは「効いてるか / 残り時間」のみ。累計系の Usage ページ拡張は別タスク (本 intent の scope 外)。

### 8.2. サブライン / 入れ子ラインの cache 戦略

メインラインは標準/連続/マニュアルがユーザー指定で動くが、サブライン (autonomous worker 等) は誰が cache モードを決めるか。
→ Phase 1-3 はメインラインのみ対応。サブラインは future scope。

### 8.3. Gemini cache の最小トークン制約 (1024)

context が 1024 tokens に満たない場合、create が失敗する。標準モードで短いコンテキストを扱う場合の挙動。
→ create 失敗時は silent fallback (= cache なしで通常コール)。エラーは log のみ。

### 8.4. 自律行動 (autonomous pulse) との連携 (将来展望)

本 intent doc の scope 外だが、自律側には既に **Anthropic 前提**の cache 延命機構が実装済みである。

**既存実装 (Phase 4-e, `sea/runtime.py:_schedule_cache_ttl_pulse`)**: anchor touch 後、explicit cache モデルなら TTL 残り `cache_threshold_ratio` の時点で meta_judgment pulse を前倒し予約する。`META_JUDGMENT_CONFIG` の `keep_cache_alive` / `cache_threshold_ratio` で制御。狙いは「Anthropic の無料 extend を利用し、1h 未満の間隔で発話し続けてペルソナを常に『起きた』状態に保つ」こと。これにより、ユーザーがいつ話しかけても cache hit の格安発話になる。

**自律行動の基本フロー (現行設計)**: 1h ごと 1 発話だけではまともな自律行動は不可能なため、メインラインの主タスクは**サブライン管理**とする。サブラインは元々安価なモデルで集中的に数十回ループを回し、メインラインが決めた目的を遂行する。メインラインは Anthropic で温度を保ちつつ高レベルの意思決定、サブラインは安価モデルで実行、という役割分担。

**Gemini への展開で生じる差異 (未解決)**: 上記の「再発話で延命」戦略は Anthropic の無料 extend が前提。Gemini は extend が時間課金されるため同じ keep-awake パターンは経済的に成立しない (§1 の逆転)。自律側で Gemini を使う場合は「集中実行 → 即 delete」を前提とした別ポリシーが要る。これは本機構の chat-UI cache とは独立した autonomous-cache intent doc で設計する。

**両立関係**: 本機構の `ICacheController` 抽象 / pulse hook は autonomous 側からも流用可能。chat-UI 側は C6 で自動 extend を持たないが、autonomous 側は Phase 4-e の通り keep-alive を持つ — 領域 (chat UI / 自律) が違うため両立する。

---

## 9. 関連ドキュメント

- [`cached_head_architecture.md`](cached_head_architecture.md) — head 安定性 (本機構の前提、実装は `sea/head_pipeline/`)
- `sea/runtime.py:_schedule_cache_ttl_pulse` (Phase 4-e) — 自律側の Anthropic cache 延命機構。本機構 (chat-UI cache) とは領域が異なる既存実装 (§8.4)
- `temp/verify_gemini_cache_billing.py` — Gemini explicit cache 実測スクリプト
- 公式: [Gemini Context Caching](https://ai.google.dev/gemini-api/docs/caching) / [Anthropic Prompt Caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)

---

## 改訂履歴

- v0.1 (2026-05-22): 起草。Gemini explicit cache の実測 (書き込み無料 / storage 課金 / extend 可能) を受けて、3 モード抽象 + キャッシュタイマー UI + provider 統一の設計を整理。既存 ChatOptions の cache toggle はマニュアルモードの実体として温存・統合。
- v0.2 (2026-05-22): まはー指摘で抜本修正。「pulse」を「session」と取り違えていた、自動 extend / idle delete を勝手に追加していた等の前提崩しを是正。標準モードは pulse 単位 (pulse 終了 delete)、連続モードは pulse 跨ぎ + TTL 任せ自然消滅、extend/delete は手動制御のみ、と明確化。cache key は `(persona_id, line_id)` に揃え、cached_head_architecture と整合。
- v0.3 (2026-05-24): 既存実装の現状調査を反映。(1) **provider で最適戦略が逆転する**核心を §1 に明文化 (Anthropic = 延命が得 / 無料 extend・storage 課金なし、Gemini = 即 delete が得 / storage 時間課金・無料 extend なし)、設計原則 P6 (戦略は provider 依存・UI は統一) を追加。(2) 自律側の既存機構 Phase 4-e (`_schedule_cache_ttl_pulse`、Anthropic 無料 extend を利用した keep-awake + サブライン管理フロー) を §8.4 に追記、Gemini 展開時の戦略差を未解決事項として明示。(3) 前提 `cached_head_architecture` が `sea/head_pipeline/` として実装済みであることを反映。(4) 用語精度修正 (`extended_cache_ttl` → 実装通り `cache_control` の `ttl`)。(5) key 粒度調査を反映: DB `line_head_snapshot` PK = `(PERSONA_ID, LINE_ID)` で doc §4.2 と一致、現状 `line_id="main"` 固定 (サブライン未配線)、1 ライン 1 モデルで provider/model は属性で足りる点を §4.2 に追記。`resolve_ttl_seconds()` 翻訳関数を §4.3 に明記。anchor(per-model) と state(per-line) の共存を §5.2 に追記。既存 global TTL (`manager.state.cache_ttl` → `_get_anchor_validity_seconds`) を per-line state に付け替える作業 (3 消費者 a/b/c) を §5.4 として新設。
- v0.10 (2026-05-25): Phase 3 (Gemini explicit cache) に着手、実機検証必須のため M1-M4 に分割。**M1 実装**: `GeminiCacheController` (create/reuse、head=system_instruction をキャッシュ、min-token ガード) + `gemini.py` 統合 (cached_content を渡し system_instruction 除去) + gemini-2.5-flash/3.5-flash-paid に cache 設定 (`gemini_explicit`)。安全のため env `SAIVERSE_GEMINI_EXPLICIT_CACHE` で全体 opt-in (cleanup/pulse-delete 未実装のため)。Gemini TTL は オフ/5分/15分/30分/1時間。test +5 (controller)。§7 Phase 3 を M1-M4 に再構成。
- v0.9 (2026-05-25): v0.8 だと 5m 書き込みのたびにタイマーが 1h にリセットされ (updated_at を毎回 now にしていた)、まはー指摘で「過大表示では」と判明。「短い書き込みが 1h キャッシュを now+1h に延命するか」は未確定 (実測は『5m では切れない』までしか証明していない)。タイマーは under-promise を優先すべき (生存と誤表示→ミスの不意打ちが最悪) ため**モデルB**を採用: 短い書き込み (新 < 既存) は window をスライドさせず起点維持、同じか長い書き込みのときだけ now にリフレッシュ。`_update_anchor_for_model` 更新、test 更新。
- v0.8 (2026-05-25): 実機で Anthropic の重要挙動が判明 — **生きているキャッシュは短い TTL で書いても短縮されない** (1h→5m→5分超経過→5m でもヒット)。公式 docs は当該ケース未記載だが「使用でリフレッシュ」は明記。v0.7 の「最後の書き込み TTL を記録」だと逆に 5m 書き込みで 1h キャッシュを短く誤表示してしまうため、`_update_anchor_for_model` を **生存中なら `max(既存, 新)` 維持・失効後リセット** に変更。これで「設定で 5m に下げても生きてる 1h は 1h 表示」= 実態と一致。§5.2 更新、test +3。
- v0.7 (2026-05-25): 実機テストで判明した遡及ズレを修正。5m→1h→5m と TTL を切り替えて書き込んだ後に設定を変えると、既存キャッシュの残り時間表示が**現行設定**で再計算され遡及的にズレていた。原因は「既存キャッシュの寿命」を `_get_anchor_validity_seconds` (現行設定) で評価していたこと。修正: 書き込み時の TTL を `METABOLISM_ANCHORS[model].ttl_seconds` に記録し、timer / head 再capture / metabolism anchor 判定は記録値を読む (`_anchor_entry_ttl_seconds`、旧 anchor は現行設定にフォールバック)。「既存キャッシュ寿命=書き込み時 TTL / 次の書き込み=現行設定」を分離。§5.2 / §5.4 更新。
- v0.6 (2026-05-24): まはー指摘で Phase 2 UI を是正。当初 per-persona TTL セレクタを既存 global cache UI に**併設**したため「TTL を決める口が 2 箇所」になり混乱 (「グローバル既定」表記も不可解)。→ cache 設定を **per-persona 1 箇所** (「キャッシュ: オフ/5分/1時間」) に統合、「データ送信量の管理」の重複 cache UI を削除。backend は per-persona を `{enabled, ttl}` に拡張し解決を `manager.resolve_persona_cache` に集約。§7 Phase 2 を更新。
- v0.5 (2026-05-24): Phase 1 (read-only タイマー UI) + Phase 2 (per-persona TTL 基盤 + §5.4 配線付け替え) 実装完了を反映。まはー判断で Phase 2 から 3 モード UI / `CacheLifecycleState` を外し Phase 3 へ移動 (Anthropic 単独ではモードが 5m/1h の言い換えに潰れ、behavioral 差は Gemini で出るため)。Phase 2 は `manager._persona_cache_ttl` override + `SEARuntime._resolve_cache_ttl_str` を単一解決点に、3 消費者 a/b/c を per-persona 化。UI はタイマーに per-persona TTL セレクタを追加。§7 Phase 2/3 を再構成。
- v0.4 (2026-05-24): まはー指摘でタイマー UI の scope を是正。**累計ヒット / 節約額はタイマーに載せない** (即時性がない → Usage ページの領分、`/api/usage/*` + `LLMUsageLog` に既存)。タイマーは「効いてるか / 残り時間」のみ。これに伴い Phase 1 を read-only (anchor から算出、state なし) に縮小、`CacheLifecycleState` 導入は Phase 2 へ移動。Phase 1 では extend/delete ボタンも出さない (Anthropic では no-op)。§1 / P3 / §4.1 / §4.3 / §4.6 / §7 / §8.1 を更新。
