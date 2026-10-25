# Intent Document: Cache Lifecycle Control

**ステータス**: Phase 1-2 実装済み (v0.5, 2026-05-24)。Phase 3 は M1 実装済み (v0.10〜v0.13)。§3.5 Gemini の継続キャッシュ (高速応答モード) は**検証待ち** (v0.14, 2026-10-11 実装・レビュー済み、実機での待ち時間の計測待ち)
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

実測で確認した通り (2026-05-22 初測定、2026-05-30 AI Studio 実課金で確定):
- **書き込み課金はない** — create 自体は無料。AI Studio 実課金 (Gemini 3.1 Pro: 40k tokens create-only → storage 分のみ ≈ 2.37 円、input 課金ゼロ) で確定。公式 pricing ページも「Google doesn't charge separately for cache creation」と明記
- 維持コストは storage = $1.00 / 1M tokens / hour (分単位 prorated、最小課金なし)
- cached hit は通常 input の 1/10
- TTL は `caches.update` で extend 可能、コンテンツ更新は不可
- `caches.list` で orphan 検出可能、`caches.delete` で明示削除可能

**create 無料の帰結 — breakeven は事実上消滅**: create が無料のため、1 回目の呼び出しから cached 価格 (1/10) で利用できる。従来の breakeven = 1.35h 計算は「create 後にキャッシュを保持し続ける」前提だったが、**毎 pulse create → 即使用 → 即 delete** (B 戦略) の場合、保持時間は数秒〜1分に収まり storage はほぼゼロ。間隔がどれだけ空こうが explicit cache が implicit を下回る。長時間保持にメリットはほぼなく、保持する唯一の利点は次回の create 遅延 (数秒〜十数秒) の回避のみ。

暗黙キャッシュ (implicit) は 1 回目を通常価格で払うため、explicit の「1 回目から 1/10」には勝てない。ただし implicit は storage 課金がなく、prefix 一致で自動的に効くため、explicit 未対応モデルや短文リクエストでは依然有用。

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
| 書き込みコスト | cache write (通常 input より割高) | **なし** (create 無料) |
| 保持コスト | なし | storage 課金 (時間比例、分単位 prorated) |
| 無料 extend | あり (再送で TTL リセット) | なし (`update` も時間課金が続く) |
| 1 回目から割引 | ×（初回は write 課金を払う） | **○（1 回目から cached price = 1/10）** |
| 最適戦略 | **延命が得**。安い再発話で cache を温め続け、idle 保持も無害 | **毎回 create → 即使用 → 即 delete が最安**。保持のメリットは create 遅延 (数秒) 回避のみ |

この差を理解した上で 3 モードを読むと意味が通る:
- **標準モード** (pulse 終了 delete) は Gemini では最適解そのもの (毎 pulse create → delete で storage を最小化)。Anthropic では delete が no-op だが storage 課金が無いので害もない
- **連続モード** (pulse 跨ぎ保持) は Anthropic では純粋に得 (保持無料 + 無料 extend)。Gemini では create 遅延を避けたい場合にのみ意味がある (コスト面では標準モードに劣る)

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
| Gemini TTL | 5 分 (create 遅延回避が目的。コスト面では短いほど得) |
| 用途 | 同じペルソナと連続して話す / 継続的な議論 |
| 振る舞い | pulse 開始で cache 作成 (既に生きてれば再利用)。pulse 終了でも **delete しない**。TTL 経過で自然消滅。ユーザーが明示的に delete / extend ボタンを押せば実行 |

pulse を跨いで cache を保持することで、連続発話の 2 回目以降を cached hit にする。自動 extend は行わない (まはー指示: 「extend は救済用、delete と同列の手動制御」)。新ターンで cache 内容を更新したい場合は、ユーザーが手動 delete してから次 pulse で新規書き込み、という運用。

**Gemini における連続モードの意味**: create が無料のため、標準モード (毎 pulse create) が常にコスト最安。連続モードで保持する唯一のメリットは **create の遅延 (数秒〜十数秒、トークン数に比例) をスキップできる**こと。コスト削減ではなくレイテンシ削減が動機。Gemini TTL は短いほど storage 節約になるため 5 分。旧 breakeven 1.35h は「保持し続ける前提」の計算であり、毎回 create → delete する B 戦略では無意味 (v0.13 で廃止)。この「create の遅延を避ける」役は、Gemini では §3.5 の継続キャッシュ (グローバル設定で選ぶ、3 モードの外の仕組み) として具体化する (v0.14)。

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

### 3.5. (3 モードの外) Gemini の継続キャッシュ (高速応答モード) — 検証待ち (2026-10-11 実装)

この節の仕組みは、§3.1〜§3.4 の 3 モード (chat タブ単位で選び、永続化しない) の一つではない。グローバル設定 (環境タブ) の「Gemini 自動キャッシュ」(`SAIVERSE_GEMINI_AUTO_CACHE`、`.env` に保存される) と同じ層にある、Gemini 専用の選択肢である。そのため §2 の P2 / P4 / P6 (UI 統一・永続化しない) と §6 の C1 / C2 (session 寿命・自動で消さない) は、自動キャッシュと同じくこの節には及ばない。§3.2 で「Gemini における連続モードの意味」とした「create の遅延を避ける」という役を、Gemini で具体化するのがこの節である。

#### なぜ要るか

自動キャッシュでは、呼び出しのたびに「システム指示 + 最新の 1 件を除いた会話全部」(`contents[:-1]`) のキャッシュを作って使い、応答の直後に消している (§7 Phase 3 の B 戦略を全呼び出しへ広げたもの、`llm_clients/gemini.py` の `_auto_cache_wrap`)。送る中身のほぼ全部がキャッシュ料金 (1/10) になるので料金は最安だが、**キャッシュを作る時間が毎回、返事を待つ時間に乗る**。

2026-10-05 の音声会話のログ (backend.log の `[gemini_cache] created` と応答の時刻) では、このキャッシュ作成に一往復ごとに 14〜27 秒かかっていた (会話に音声・動画・画像が多く入っていたため)。返事の生成そのもの (13〜25 秒) と合わせて、話しかけてから声が返るまで 35〜60 秒になっていた。音声会話のように待ち時間がそのまま体験を壊す場面では、料金よりも速さを取りたい。

#### 言葉の定義

- **一往復**: ユーザーの発言 1 件に対して、ペルソナが返事をする Pulse 1 回。1 回の Pulse の中で LLM を複数回呼ぶこと (スペルの往復など) がある。
- **キャッシュの境目**: その Pulse の履歴 (SAIMemory から読み出した部分) の中で、**最後の AI の返事の直後**。Pulse が始まる前に確定している位置なので、同じ Pulse の中の何回目の呼び出しでも同じ位置になる。送るメッセージの並び (`contents`) の中の「最後の `model` ロール」ではない — 2 回目以降の呼び出しでは、それがその Pulse の途中の発言を指してしまうため。

#### キャッシュの境目をここに置く理由

ターンをまたいで中身が同じになるのは、**境目 (n 回目の Pulse では AI の返事(n-1) の直後) まで** である。その後ろに何が来るかは場面で変わる: ユーザーの発言、知覚のブロック、リアルタイム情報 (`_build_realtime_context`)、自動想起 (`_maybe_inject_auto_recall`)、その Pulse の中のスペルの往復。リアルタイム情報と自動想起は、送るたびに「最後のユーザー役のメッセージの直前」へその場で差し込まれ、次のターンには残らない (`sea/runtime_context.py`)。(注記 2026-10-14: モデル設定 `cache.persist_realtime_and_recall` を ON にしたモデルでは、この二つを一つのまとまりにして送る列の一番後ろに置き、送った文面のまま残して次のターン以降も同じ位置に送る — [persist_realtime_and_recall.md](persist_realtime_and_recall.md)。境目を指定できないモデル向けの対応で、境目を指定できる継続キャッシュの対象モデルでは OFF のまま使う想定。) 知覚のブロックは、読んだ時刻の位置に入る。並びの例:

```
n 回目:   … / AIの返事(n-1) ‖ ユーザーの発言(n) / [リアルタイム情報・自動想起・知覚・スペルの往復は場面次第でこの辺りに入る]
n+1 回目: … / AIの返事(n-1) / ユーザーの発言(n) / (知覚) / AIの返事(n) ‖ ユーザーの発言(n+1) / [同上]
```

(`‖` が各回の境目)

自動キャッシュのキャッシュ (`contents[:-1]`) は、リアルタイム情報か自動想起が入る回には、その回の分まで含んでしまう。だから n+1 回目の並びの先頭とは一致しない (保持秒数を 1 以上にしても再利用されない理由。`docs/issues/gemini_auto_cache_no_reuse_within_ttl.md`)。

境目より前でも、次のことが起きると中身が変わり、キャッシュと並びの先頭が一致しなくなる。

- **人格の指示文 (head) の作り直し**: Metabolism のほか、`cached_head_architecture.md` §3.6 の `refresh_on_events` (部屋の移動・スペルの切り替え・モデルの変更・システムプロンプトの編集など) と、anchor の TTL 切れ (最後の LLM 呼び出しから Gemini は既定 1200 秒、何も呼ばれなかったとき) で起きる。
- **画像のバイトの非常弁**: 画像は既定では枚数で絞らず、すべて画像のまま送る。送る画像の合計 (base64 後の見積もり) が天井 (既定 12MB) を超える送信でだけ、古い画像を床 (既定 6MB) まで要約の文字にまとめて置き換える (`llm_clients/utils.py` の `select_embedded_attachment_keys`)。この回だけ境目より前の古い画像が文字に変わる。列の後ろに足すだけの間は、一度文字にした画像は以後も文字のままなので、弁が開かない回は前方一致が割れない (それ一枚で天井を超える画像は、他の画像を巻き込まずにその一枚だけが文字になる)。枚数の上限を env (`SAIVERSE_{PROVIDER}_ATTACHMENT_LIMIT` / `SAIVERSE_ATTACHMENT_LIMIT`) やモデル設定の `max_image_embeds` で明示した場合は、以前と同じく新しい画像が入るたびに一番古い画像が文字に変わる (`docs/issues/image_attachment_limit_breaks_cache_prefix.md`)。

#### 一往復の流れ (n 回目の Pulse)

1. 送るメッセージの並びを組み立てる。境目の位置も一緒に決まる。
2. 生きているキャッシュのうち、入れた範囲の中身が今回の並びの先頭と一致するものを探す。n-1 回目に作った「AI の返事(n-2) まで」のキャッシュが一致すれば、それを `cached_content` に指定し、キャッシュに入っていない分 (ユーザーの発言(n-1) から最後まで) だけを送る。同じ Pulse の中の 2 回目以降の呼び出しも、同じキャッシュを使う。
3. その Pulse の最初の呼び出しと**並行して**、「AI の返事(n-1) まで」(今回の境目まで) の新しいキャッシュを作り始める。この範囲は Pulse が始まる前に確定しているので作れる。作成の完了は待たずに返事を依頼する。
4. 新しいキャッシュが出来上がったら、古いキャッシュを消す。

一致の判定は、キャッシュに入れた範囲の中身全体と、今回の並びの先頭の同じ範囲を比べて行う。比べる中身には、文字だけでなく画像・音声・動画のバイト列そのもの (長さではなく中身) と、ツール呼び出しの往復を含める。今の `GeminiCacheController._content_signature` は画像などを `<mime:バイト長>` で表すだけなので、この判定には使えない。こうしておけば、予想が外れても、合わないキャッシュを使うことはない。

#### 例外

- **一致するキャッシュが無い** (会話の最初の一回、head の作り直し、新しい画像が入って古い画像が文字に変わった回、など): 「今回の境目まで」のキャッシュを先に作ってから送る。この回だけは作成の時間を待つ。古いキャッシュは、新しいキャッシュが出来た時点で消す。
- **新しいキャッシュの作成が次の発言に間に合わない**: 古いキャッシュはまだ消していないので、それを使い、キャッシュに入っていない分を多めに送る。作成は待たない。
- **付けたキャッシュを Gemini が使えないと返した** (サーバー側で消えた・生成の途中で失効した・見つからない、など): キャッシュは無くても返事を作れるので、その回だけキャッシュを外して (contents 全部と system_instruction を戻して) 1 回だけ送り直し、そのキャッシュは二度と使わない。ストリーミングでは、まだ何も返していないときだけ送り直す。自動キャッシュ・モデル設定の explicit cache も同じ扱い (2026-10-11 の敵対レビューの指摘で追加)。

保存料金は「作ったときに TTL の全額を記録し、TTL より前に消したら残りを負の記録で差し引く」予約席の約束 (v0.13) に従う。継続キャッシュは作った時点でその要求のペルソナ・建物で記録し、置き換えなどで早く消したときは残りの秒数分を差し引く。自動キャッシュの側に残る同じ形の穴は `docs/issues/gemini_auto_cache_storage_accounting_gaps.md`。

#### 料金と速さの交換 (自動キャッシュとの違い)

| | 自動キャッシュ | 継続キャッシュ (高速応答モード) |
|---|---|---|
| キャッシュに入れる範囲 | 最新の 1 件を除いた全部 | その Pulse の境目まで (最新の AI の返事まで) |
| キャッシュを作るタイミング | 返事の依頼の直前 (待ち時間に乗る) | 返事の依頼と並行 (次の往復のため。待ち時間に乗らない) |
| キャッシュに入っていない分 | 最新の 1 件だけ | 前の往復 + 今回の分 (**通常料金**) |
| 保存料金 | ほぼゼロ (応答直後に消す) | 会話中は 1〜2 本を保持する分 |

キャッシュに入っていない「前の往復 + 今回の分」がキャッシュ料金にならない分、自動キャッシュより毎回高くなる。だから両者は一長一短で、ユーザーが選ぶ。

#### 選び方と効く範囲

まはーの判断 (2026-10-11):

- 自動キャッシュとは一長一短なので、切り替えを作る。自動キャッシュの隣に置く。名称は「継続キャッシュ (高速応答モード)」(まはー案。§3.2 の「連続モード」との近さを示したうえで、このまま決定)。
- 効かせるのは会話だけ。会話以外の呼び出しは、継続キャッシュを選んでいても自動キャッシュと同じ動きで送る (書き手の推奨にまはーが同意)。
- 自動キャッシュの「応答後に何秒残すか」の設定は、そのまま残す。
- 継続キャッシュの寿命は利用者が変えられる設定にする。既定 5 分、上限 20 分 (書き手の推奨にまはーが同意)。下限は 2 分 (実装時の補い — 失効の 30 秒前から使わなくなるので、1 分だと使える時間が 30 秒しか残らない)。
- 画像の枚数上限はそのまま。新しい画像が入った回はキャッシュの作成を待つ (書き手の推奨にまはーが同意。上限そのものの扱いは `docs/issues/image_attachment_limit_breaks_cache_prefix.md`)。

これを具体にすると次のとおり (書き手の補い):

- グローバル設定 (環境タブ) の選択肢は「オフ / 自動キャッシュ / 継続キャッシュ」の三つで、どれか一つを選ぶ。継続キャッシュを選んだときも、会話の返事以外の呼び出しは、自動キャッシュを選んだときと同じ動き (保持秒数の設定も含めて) をする。つまり継続キャッシュは「自動キャッシュ + 会話の返事だけ別の送り方」である。
- 「会話の返事」は、ユーザーの発言に返事をする Pulse (`pulse_type="user"`) の LLM 呼び出しを指す。schedule の Pulse とペルソナ同士の会話は含めない (CONVERSATION アスペクトはこれらも含むので、アスペクトでは判定しない)。どの呼び出しが会話の返事かと、境目の位置は、SEA の側から Gemini のクライアントへ渡す。クライアントの中では、その呼び出しが何のためのものかを判別できないため。
- 継続キャッシュの各キャッシュは、作るときに TTL を付ける。TTL は「作ってから何分で消えるか」で、作成は返事と並行して始まるので、最後の往復の返事が終わってから数えるより、返事の生成時間の分だけ早く消える。TTL の上限を 20 分にするのは、最後の LLM 呼び出しから anchor の TTL (Gemini 既定 1200 秒、`session_lifecycle.get_anchor_validity_seconds`) が過ぎると、次の呼び出しで head が作り直されてキャッシュが一致しなくなるため。

#### 明示キャッシュに対応しないモデル (2026-10-14 追加)

モデル設定で `cache.explicit_supported: false` としたモデル (2026-10 時点で Gemini 4 Argon) では、継続キャッシュ・自動キャッシュ・モデル設定の explicit cache のどれを選んでいても、キャッシュの作成 (`caches.create`) を一切試みず、Google 側の暗黙キャッシュ (implicit) だけで動く。根拠は二つで、公式文書が Argon について「explicit caching (`caches.create` / `cached_content`) は非対応、implicit caching のみ」と明記していることと、Models API の `supported_actions` が `generateContent` / `countTokens` / `batchGenerateContent` だけで、他の Gemini にある `createCachedContent` を含まないこと。作成を試みても毎回拒否されるだけなので、`llm_clients/gemini.py` の `_apply_request_cache` の入口で三つの経路をまとめて止める。省略時は `true` (従来どおり)。

暗黙キャッシュでは境目をこちらから指定できないので、毎ターン消えるリアルタイム情報と自動想起が末尾近くにあると、前回の送信と今回の送信の先頭が一致しなくなる。これに対する対応は別の設定 `cache.persist_realtime_and_recall` で行う (Argon では ON。設計は [persist_realtime_and_recall.md](persist_realtime_and_recall.md))。

#### 実装の置き場 (見込み)

- `llm_clients/gemini_cache.py`: キャッシュごとに「先頭から何件を入れたか」と、その範囲の中身全体の署名 (上の「一致の判定」の粒度) を覚え、今回の並びの先頭と一致するキャッシュを探す処理を足す。今は中身全体のハッシュ 1 個で完全一致しか見ていない。
- `llm_clients/gemini.py`: `_auto_cache_wrap` の隣に継続キャッシュの経路を置き、受け取った境目までのキャッシュの検索・並行作成・古いキャッシュの削除を行う。
- `sea/` 側: 会話の返事の呼び出しであることと、境目の位置 (履歴の中の最後の AI の返事) を、Gemini のクライアントへ渡す。
- `api/routes/config.py` の `/api/config/gemini-auto-cache` と、グローバル設定の画面 (`frontend/src/components/GlobalSettingsModal.tsx`) に選択肢を足す。新しい環境変数は環境変数の一覧 (`docs/reference/environment-vars.md`) にも足す。

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

- **M1 (✅ 実装済み 2026-05-25)**: Gemini explicit cache の配線。**キャッシュ範囲は B 戦略**
  - **キャッシュ対象 = system_instruction + contents の「最新入力を除く全部」** (`contents[:-1]`)、リクエストは最新の1メッセージ (`contents[-1:]`) だけ送る。初期実装は system_instruction のみだったが、実機で「7651/18937 しか効かない」= head が system_instruction と contents 先頭 (memory_weave/visual) + 会話履歴に跨るのに system しかキャッシュしてなかった。Anthropic は `cache_control` を末尾に置いて head+履歴をキャッシュしている → Gemini も同等にするため B (最新入力以外を全部キャッシュ、毎ターン create) を採用 (まはー判断: create 無料なのでヒット最大化、cleanup は必須なのでどうせ作る)
  - `llm_clients/gemini_cache.py`: `GeminiCacheController` (create/reuse、`(model, sha256(system_instruction + contents 署名))` キー、文字数ベースの最小トークンガード、in-memory)
  - `gemini.py`: `generate`/`generate_stream` が `enable_cache`/`cache_ttl` を受け、`_resolve_explicit_cache` で `(cache_name, 送信 contents)` を解決。張れたら `cached_content` を渡し **system_instruction を除去**・**contents を tail に絞る**
  - `gemini-2.5-flash.json` / `gemini-3.5-flash-paid.json` に cache 設定 (`type: "gemini_explicit"`, ttl_options, min_tokens=1024)
  - **安全ゲート**: env `SAIVERSE_GEMINI_EXPLICIT_CACHE` で全体 opt-in (cleanup/pulse-delete 未実装のため。M2-M4 後に撤去)。検証は log の `cached_content_token_count` (prefix 全体が乗る)
  - 既知の制限: 毎ターン create する (prefix が伸びるため) → 古い cache が溜まる。TTL (5m) で自然失効するが、即時の supersede delete / orphan cleanup は M2
  - **重要な発見 (2026-05-25 実機 + 公式 docs 確定)**: Gemini explicit cache は **text + media はキャッシュするが thought_signature (思考署名) はキャッシュしない**。Gemini 3.5 Flash は thought preservation がデフォルト ON で過去ターンの thinking を毎回再送するため、その分 (実測 ~10,579 tok) が**キャッシュ不可で毎ターン課金**される。B のバグではなく Gemini 仕様。制御は「過去ターンの thought_signature を送信前に剥がす」のみ (公式が認める唯一の手段、現ターンの function-call 署名は剥がすと 400)。→ 別 issue `docs/issues/gemini_multiturn_reasoning_thought_signature_uncached.md`
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

### 8.2. provider 統一抽象の見直し (2026-05-30 検証で浮上)

create 無料の確定 (v0.13) により、Gemini と Anthropic の最適戦略が根本的に分岐した:

| | Anthropic | Gemini |
|---|---|---|
| 核心 | write 課金あり → **reuse 最優先** | create 無料 → **freshness 最優先** |
| pulse 内 | cache を温存して hit を稼ぐ | 毎 LLM コールで create し直す方が中間トークンも 1/10 で得 (要 delete) |
| pulse 間 | 延命 (無料 extend) が最安 | 保持する意味がほぼない (create 遅延回避のみ) |
| TTL の意味 | 延命の器 (長いほど得) | 異常終了時の orphan 保険 (短い方が安全) |

現在の 3 モード (標準 / 連続 / マニュアル) は「TTL 戦略の違い」で抽象化しているが (§2 P1)、Gemini の最適解は TTL ではなく **create/delete の粒度** で決まる。同じ「標準モード」の中身が provider で全く異なる概念を指すことになり、統一抽象が形骸化する。

検討すべき方向性:
- **(a)** モード名を provider 非依存に保ちつつ、backend の振る舞いを provider ごとに完全に分ける (UI は統一、実装は分岐)
- **(b)** provider ごとに別の戦略体系を持ち、UI も分ける (P2 の撤回)
- **(c)** Gemini 向けに「毎コール create/delete」モードを新設し、Anthropic の reuse 系モードと並存させる

いずれも session.md の統合議論 (§6) と合わせて検討する。

**その後の Gemini 側の動き (v0.14 時点)**: Gemini では、3 モードの抽象を待たずに、グローバル設定で選ぶ仕組みが二つ並ぶ形になった — 毎コール create/delete の「自動キャッシュ」(`SAIVERSE_GEMINI_AUTO_CACHE`) と、create の遅延を待ち時間から外す「継続キャッシュ」(§3.5)。方向性 (c) に近いが、3 モード (chat タブ単位) との統合はまだしていない。

### 8.3. サブライン / 入れ子ラインの cache 戦略

メインラインは標準/連続/マニュアルがユーザー指定で動くが、サブライン (autonomous worker 等) は誰が cache モードを決めるか。
→ Phase 1-3 はメインラインのみ対応。サブラインは future scope。

### 8.4. Gemini cache の最小トークン制約 (1024)

context が 1024 tokens に満たない場合、create が失敗する。標準モードで短いコンテキストを扱う場合の挙動。
→ create 失敗時は silent fallback (= cache なしで通常コール)。エラーは log のみ。

### 8.5. 自律行動 (autonomous pulse) との連携 (将来展望)

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

- v0.15 (2026-10-14): §3.5 に「明示キャッシュに対応しないモデル」を追加。モデル設定 `cache.explicit_supported: false` のモデル (Gemini 4 Argon) では、自動キャッシュ・継続キャッシュを選んでいてもキャッシュの作成を試みず、暗黙キャッシュだけで動く。
- v0.14 (2026-10-11): §3.5 「Gemini の継続キャッシュ (高速応答モード)」を追加。音声会話でキャッシュの作成 (一往復 14〜27 秒) が待ち時間に乗っていたことから、まはーと設計した。キャッシュの境目は「その Pulse の履歴の中の最後の AI の返事の直後」(同じ Pulse の何回目の呼び出しでも同じ位置)、n 回目は一つ前に作ったキャッシュを使い、次の往復用のキャッシュを返事と並行して作る。キャッシュに入っていない分が通常料金になるため自動キャッシュとは一長一短で、グローバル設定でどちらか一方を選ぶ。効かせるのは会話の返事だけ。自動キャッシュの保持秒数の設定は残す (まはー判断)。
- v0.1 (2026-05-22): 起草。Gemini explicit cache の実測 (書き込み無料 / storage 課金 / extend 可能) を受けて、3 モード抽象 + キャッシュタイマー UI + provider 統一の設計を整理。既存 ChatOptions の cache toggle はマニュアルモードの実体として温存・統合。
- v0.2 (2026-05-22): まはー指摘で抜本修正。「pulse」を「session」と取り違えていた、自動 extend / idle delete を勝手に追加していた等の前提崩しを是正。標準モードは pulse 単位 (pulse 終了 delete)、連続モードは pulse 跨ぎ + TTL 任せ自然消滅、extend/delete は手動制御のみ、と明確化。cache key は `(persona_id, line_id)` に揃え、cached_head_architecture と整合。
- v0.3 (2026-05-24): 既存実装の現状調査を反映。(1) **provider で最適戦略が逆転する**核心を §1 に明文化 (Anthropic = 延命が得 / 無料 extend・storage 課金なし、Gemini = 即 delete が得 / storage 時間課金・無料 extend なし)、設計原則 P6 (戦略は provider 依存・UI は統一) を追加。(2) 自律側の既存機構 Phase 4-e (`_schedule_cache_ttl_pulse`、Anthropic 無料 extend を利用した keep-awake + サブライン管理フロー) を §8.4 に追記、Gemini 展開時の戦略差を未解決事項として明示。(3) 前提 `cached_head_architecture` が `sea/head_pipeline/` として実装済みであることを反映。(4) 用語精度修正 (`extended_cache_ttl` → 実装通り `cache_control` の `ttl`)。(5) key 粒度調査を反映: DB `line_head_snapshot` PK = `(PERSONA_ID, LINE_ID)` で doc §4.2 と一致、現状 `line_id="main"` 固定 (サブライン未配線)、1 ライン 1 モデルで provider/model は属性で足りる点を §4.2 に追記。`resolve_ttl_seconds()` 翻訳関数を §4.3 に明記。anchor(per-model) と state(per-line) の共存を §5.2 に追記。既存 global TTL (`manager.state.cache_ttl` → `_get_anchor_validity_seconds`) を per-line state に付け替える作業 (3 消費者 a/b/c) を §5.4 として新設。
- v0.13 (2026-05-30): **create 無料を AI Studio 実課金で確定** (Gemini 3.1 Pro 40k tokens create-only → input 課金ゼロ、storage 分 ≈ 2.37 円のみ / Gemini 3 Flash create+generate×2 → ≈ 1 円)。公式 caching docs / pricing と完全一致。これにより Gemini の最適戦略が確定: **毎 pulse create → 即使用 → 即 delete が最安** (1 回目から 1/10 価格、保持のメリットは create 遅延回避のみ)。旧 breakeven 1.35h は保持前提の計算で B 戦略では無意味 → 廃止。§1 Gemini 特性、§1 provider 逆転テーブル、§3.2 連続モード Gemini TTL を更新。**storage 計上を Usage に実装**: `calculate_cache_storage_cost()` (model_configs.py)、`CacheEnsureResult` (gemini_cache.py)、`record_cache_storage()` (usage_tracker.py)、runtime_llm.py 全 5 経路で `_maybe_record_cache_storage` 呼び出し。TTL 全体を予約席方式で先払い計上 (CATEGORY="cache_storage" の独立レコード)、将来の delete 時に残 TTL 分を負計上で返金する設計。pricing に `cache_storage_per_1m_tokens_per_hour` を追加 (gemini-3.5-flash-paid: $1.0)。
- v0.12 (2026-05-25): 実機検証で B が正しく動作 (text+media を prefix キャッシュ、tail のみ送信) と確定。残った「~10,579 非キャッシュ」は **Gemini が thought_signature をキャッシュしない + 3.5 Flash の thought preservation デフォルト ON** が原因と判明 (公式 docs 確定)。B のバグではない。マルチターン推論 ON/OFF (過去 thought_signature の剥がし) を別 issue 化。§7 M1 に追記。
- v0.11 (2026-05-25): 実機で M1 (system_instruction のみキャッシュ) が 7651/18937 しか効かないと判明。SAIVerse の head は system_instruction と contents 先頭 (memory_weave/visual) + 会話履歴に跨り、Anthropic は末尾 `cache_control` で head+履歴をキャッシュしているのに Gemini は system しか乗せていなかった。→ **B 戦略**に変更: `system_instruction + contents[:-1]` (最新入力以外全部) をキャッシュ、リクエストは `contents[-1:]` のみ。毎ターン create (Gemini create は無料)、古い cache は TTL + cleanup(M2) で処理。`GeminiCacheController.ensure` に contents 対応、key を sys+contents 署名に。test +2。
- v0.10 (2026-05-25): Phase 3 (Gemini explicit cache) に着手、実機検証必須のため M1-M4 に分割。**M1 実装**: `GeminiCacheController` (create/reuse、head=system_instruction をキャッシュ、min-token ガード) + `gemini.py` 統合 (cached_content を渡し system_instruction 除去) + gemini-2.5-flash/3.5-flash-paid に cache 設定 (`gemini_explicit`)。安全のため env `SAIVERSE_GEMINI_EXPLICIT_CACHE` で全体 opt-in (cleanup/pulse-delete 未実装のため)。Gemini TTL は オフ/5分/15分/30分/1時間。test +5 (controller)。§7 Phase 3 を M1-M4 に再構成。
- v0.9 (2026-05-25): v0.8 だと 5m 書き込みのたびにタイマーが 1h にリセットされ (updated_at を毎回 now にしていた)、まはー指摘で「過大表示では」と判明。「短い書き込みが 1h キャッシュを now+1h に延命するか」は未確定 (実測は『5m では切れない』までしか証明していない)。タイマーは under-promise を優先すべき (生存と誤表示→ミスの不意打ちが最悪) ため**モデルB**を採用: 短い書き込み (新 < 既存) は window をスライドさせず起点維持、同じか長い書き込みのときだけ now にリフレッシュ。`_update_anchor_for_model` 更新、test 更新。
- v0.8 (2026-05-25): 実機で Anthropic の重要挙動が判明 — **生きているキャッシュは短い TTL で書いても短縮されない** (1h→5m→5分超経過→5m でもヒット)。公式 docs は当該ケース未記載だが「使用でリフレッシュ」は明記。v0.7 の「最後の書き込み TTL を記録」だと逆に 5m 書き込みで 1h キャッシュを短く誤表示してしまうため、`_update_anchor_for_model` を **生存中なら `max(既存, 新)` 維持・失効後リセット** に変更。これで「設定で 5m に下げても生きてる 1h は 1h 表示」= 実態と一致。§5.2 更新、test +3。
- v0.7 (2026-05-25): 実機テストで判明した遡及ズレを修正。5m→1h→5m と TTL を切り替えて書き込んだ後に設定を変えると、既存キャッシュの残り時間表示が**現行設定**で再計算され遡及的にズレていた。原因は「既存キャッシュの寿命」を `_get_anchor_validity_seconds` (現行設定) で評価していたこと。修正: 書き込み時の TTL を `METABOLISM_ANCHORS[model].ttl_seconds` に記録し、timer / head 再capture / metabolism anchor 判定は記録値を読む (`_anchor_entry_ttl_seconds`、旧 anchor は現行設定にフォールバック)。「既存キャッシュ寿命=書き込み時 TTL / 次の書き込み=現行設定」を分離。§5.2 / §5.4 更新。
- v0.6 (2026-05-24): まはー指摘で Phase 2 UI を是正。当初 per-persona TTL セレクタを既存 global cache UI に**併設**したため「TTL を決める口が 2 箇所」になり混乱 (「グローバル既定」表記も不可解)。→ cache 設定を **per-persona 1 箇所** (「キャッシュ: オフ/5分/1時間」) に統合、「データ送信量の管理」の重複 cache UI を削除。backend は per-persona を `{enabled, ttl}` に拡張し解決を `manager.resolve_persona_cache` に集約。§7 Phase 2 を更新。
- v0.5 (2026-05-24): Phase 1 (read-only タイマー UI) + Phase 2 (per-persona TTL 基盤 + §5.4 配線付け替え) 実装完了を反映。まはー判断で Phase 2 から 3 モード UI / `CacheLifecycleState` を外し Phase 3 へ移動 (Anthropic 単独ではモードが 5m/1h の言い換えに潰れ、behavioral 差は Gemini で出るため)。Phase 2 は `manager._persona_cache_ttl` override + `SEARuntime._resolve_cache_ttl_str` を単一解決点に、3 消費者 a/b/c を per-persona 化。UI はタイマーに per-persona TTL セレクタを追加。§7 Phase 2/3 を再構成。
- v0.4 (2026-05-24): まはー指摘でタイマー UI の scope を是正。**累計ヒット / 節約額はタイマーに載せない** (即時性がない → Usage ページの領分、`/api/usage/*` + `LLMUsageLog` に既存)。タイマーは「効いてるか / 残り時間」のみ。これに伴い Phase 1 を read-only (anchor から算出、state なし) に縮小、`CacheLifecycleState` 導入は Phase 2 へ移動。Phase 1 では extend/delete ボタンも出さない (Anthropic では no-op)。§1 / P3 / §4.1 / §4.3 / §4.6 / §7 / §8.1 を更新。

### 日付別料金と保管費の見積もり（2026-10-10）

TTL 前払いは維持し、作成時刻から終了までに料金境界があれば各区間を時間按分する。
共通の期間解決・Gemini 割引の保守的終了時刻・過去記録を再計算しない方針は
[モデル料金の契約](model_provider_management.md#日付による料金切替2026-10-10)を参照。
継続キャッシュは、サーバーの失効時刻から TTL 分さかのぼった区間を前払いし、
削除成功から区間末尾までの未使用部分を同じ日付料金で返金する。時計差で削除が
予約開始より前になった場合も、返金区間は元の予約内へ丸める。削除失敗や自然失効は返金しない。
自動キャッシュの返金は未実装のままで、この変更で実請求との一致を保証するものではない。
