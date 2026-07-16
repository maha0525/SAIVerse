# Intent: Beat と ExecutionContext — SEA 実行基盤の一本化

**ステータス**: 実装待ち (v0.2, 2026-07-16) — まはーレビュー 1 巡で全論点解決（知覚バッファ経由の明記・常時通知への単純化・割り込みは Beat 境界待ち・Metabolism 閾値はモデル依存・未整理 section も本工事に包含）
**位置付け**: [`execution_ledger.md`](execution_ledger.md)（柱1）と対を成す SEA 側の工事。同じ場所（SEA runtime の実行単位）に住む 4 つの問題を、一度の掘り返しで解く。
**前提**: [`session.md`](session.md)（正典: Session 粒度は (persona, model)。**本 intent は同 doc の「head は line×model」記述を改訂する**）/ [`cached_head_architecture.md`](cached_head_architecture.md) / [`dynamic_state_sync.md`](dynamic_state_sync.md)（B = A + Σ(events)。**本 intent は通知を操作ラベル型から内容型へ改める**）/ [`persona_cognition/line_tag_responsibility.md`](persona_cognition/line_tag_responsibility.md) §10 aspect
**監査対応**: SEA 監査 S1（実行 model 無視の Session/anchor 更新）・S4（Stelis thread 復元漏れ）・S8（anchor 並列 RMW）・S2/M1（Metabolism、実行台帳と共同）/ issue [`head_mutation_notification_gap.md`](../issues/head_mutation_notification_gap.md)

---

## 1. 何を解くか — 同じ場所に住む 4 つの問題

1. **柱 2: Session の (persona, model) 正典に実装が追いついていない**。head snapshot の物理キーに model が無く、anchor/TTL/Metabolism 窓は `persona.model` 固定で、lightweight 呼び出しが standard の cache 寿命を偽延命する（S1）。まはーの診断どおり「設計はフルキー、実装が途中で止まっていた」。
2. **柱 1 Phase 0 の実装点**: Beat ロック（記憶書き込みの persona 単位直列化、execution_ledger §2.3）と execution_id の持ち回りを、SEA runtime のどこに置くか。
3. **S4**: active thread が persona 共有の可変ファイルで、Stelis 中断時に親へ戻らない。
4. **head 操作の通知の穴**: 「本人の操作は通知不要」という単一窓前提が、line 隔離・model 分離で破れている（issue 参照。META の生きる目的書き換えは現行で内容不達）。

4 つとも根は同じ — **「今この実行が何者か」を持ち回る器が無く、各層が persona の可変属性から都度推測している**。

## 2. 中核概念

### 2.1 ExecutionContext — 実行の身分証

Beat の開始時に一度だけ解決し、以後すべての層が読むだけの不変の器。

```
ExecutionContext:
    persona_id      # 誰の実行か
    thread_id       # どの thread に記録するか (Stelis は push/pop、S4 の解)
    line_id         # main / sub / meta — 生ログの line_role の供給元
    aspect          # CONVERSATION / WORKER / AUTONOMOUS / META (§10 aspect)
    model_key       # aspect から導出した実行 model。Session の帰属先
    pulse_id        # 所属 Pulse
    execution_id    # 実行台帳の ID (柱1)。台帳に載らない軽量 Beat は None 可
```

- **解決は Beat 開始時に一度**。context 構築・LLM client 選択・使用量記帳・anchor touch・Metabolism・記録のすべてが同じ器から読む。`persona.model` の再推測、`history_manager` の persona 単一可変属性への後読みを全廃する。
- LLM 呼び出し後は実際の使用量記録の model と照合し、structured-output fallback 等で実行 model が変わった場合は**実 model 側の Session** に記帳する（S1 の「usage.model を無視して standard を touch」の根治）。
- thread_id は ExecutionContext の値が正で、Stelis はスタック的に push/pop する。graph の成功・例外・cancel すべての `finally` で親へ戻す（persona 共有ファイルは廃止または「起動時復元用の鏡」に格下げ）。

### 2.2 Beat — 型を持つ最小行動単位

execution_ledger §2.3 で定義した「関所（pending flush）→ コンテキスト読み → 1 回の生成 → 記録」の一続き。既存コードでは spell ループ 1 周が実体。本工事で初めて型（ExecutionContext を保持する実行単位）になる。

- **persona 単位の Beat ロック**で直列化。main 会話・META 判断・自律・作業セッションの各 Beat は交互に挟まり、同時には走らない（不変条件: 記憶の一直線性）。
- PulseController の main/META 並行 submit は解体し、META lane は「main の Beat 境界で挟まる直列 Beat」になる。

## 3. 設計

### 3.1 Session の (persona, model) フルキー化

- **head snapshot**: 物理キーを `(persona_id, model_key)` にする（in-memory / DB とも）。既存行は記録済みの `MODEL_KEY` 列を新キーへ migration。
- **head のキーに line は含めない**（まはー裁定 2026-07-16）: line で head を分けると prefix cache の共用という head の存在意義が死ぬ。サブラインも同じ model なら同じ head を共有する。`session.md` の「設計上は line×model」記述はこの裁定に合わせて改訂する。line 隔離で生じる情報格差は §3.3 の内容型通知が埋める。
- **anchor / TTL / token threshold**: `(persona_id, model_key)` の行へ正規化し、model 単位 upsert にする（S8 の JSON 全体 read-modify-write を廃止）。Beat 直列化により並列競合自体も消えるが、構造として行単位を正とする。
- **TTL/keep-alive watchdog の予約キー**にも model を含め、Session ごとに独立監視する。
- **diff 通知の既読状態（last_notified）**も `(persona_id, model_key)` で分離する — 各 Session が「自分がまだ知らない変化」を独立に受け取るため。

### 3.2 Metabolism の二層分離 — 編纂は persona に一度、退役は model ごと

- **編纂（Chronicle 生成）**: persona 共有の土地の仕事。冪等キー `(metabolism.run, persona:窓)` で実行台帳に claim し、全入口（各 model の Metabolism・session close・手動整理・API）が同じ排他を通る（M1 の解）。
- **退役（anchor 前進）**: model ごと。自 model の窓が節目を迎えた時だけ、**退役範囲の編纂が済んでいることを確認してから** anchor を進める（S2 の解 — 編纂失敗時は据え置いて次回再試行）。
- **可視化は model の節目ごと**: 各 model のコンテキストに入る Chronicle 集合は、その model の anchor 更新時に確定する。他 model の編纂で新しい Chronicle ができても、自分の節目までは prefix に入れない（prefix cache 保護）。窓が生ログでカバーしている間は情報欠落はなく、退役の瞬間に退役分の Chronicle が入れ替わりに見える。
- **Metabolism の閾値（watermark / token threshold）はモデル依存**（まはー確認 2026-07-16）: standard/lightweight という区別ではなく、実行 model のモデル設定（context_length 等）から導出する。各 Session は自分の model の閾値で自分の窓を管理する。
- `history_manager.metabolism_anchor_message_id`（persona 単一可変属性）は廃止し、ExecutionContext 経由で解決した model 別 anchor を使う。TTL 失効時の minimal load 後に旧 anchor を touch する事故（記憶監査第 4 片）も、call-local に「今回組成した prefix の anchor」を渡すことで消す。

### 3.3 head 操作の内容型通知（issue `head_mutation_notification_gap` の解）

> **head の元データへの操作は、常に、その persona の全 Session 窓へ「head に入るときと同一の render 断片」を内容型通知として配送する。**

- **内容忠実性の構造的保証**: 通知本文は section の render と同一の関数から生成する。「head で見える内容」と「tail 通知で届く内容」の文面を別々に書かない（書き分けた瞬間からドリフトが始まる）。
- **通知要否の判定は持たない — 常に通知する**（まはー裁定 2026-07-16）: 「読み手の窓に操作の生ログが残るか」という判定は作らない。main 以外の line での操作は、操作した本人にも結局通知が届く — そのペルソナ体験を判定ロジックで完璧に対策しきれない以上、無理に効率化しない。安全と単純を優先する。main line で撃った直後は spell 結果の生ログと通知が二重に見えるが、これも引き受ける。
- **操作起点の push 型**: 判定を持たないため、通知は snapshot 差分の検出（flush_diffs）に依存せず、**操作したツール（memory_write / memory_open / life_purpose_set 等）が成功時に自分で発行**する（execution の副作用の一つとして outbox へ）。snapshot 差分比較は、ツールを経由しない変化（ユーザーの UI 編集・migration 等）を拾う backstop に退く。
- **配送経路は「outbox → 知覚バッファ → tail」の二段**（まはー確認 2026-07-16）: outbox は配達保証の層（world DB、柱 1）、知覚バッファ（`push_perception` / adapter の durable buffer）はペルソナの知覚の入口という既存の正典で、既存 diff 通知もここを通っている。本通知も同じ入口を通す。S5（知覚バッファの flush 失敗で全消失）・S3（配送前の last_notified 前進）の修正と同一経路がまとめて堅牢化される。通知の既読状態（last_notified）は (persona, model) 分離 — フルキー化の一部。
- 対象 section: コア記憶・机・生きる目的・Memopedia 目次（opt-in 時）・**memory_weave・chronicle_index**（旧 Phase 3 積み残しの diff 未整理も本工事で潰す。まはー裁定:「後に残してもあんま良いこと無い。結局全部やる作業なんだから分かってる時点で潰す」）。先例は visual_context（2026-07-09 の「中身ごと届ける」改修）と building（system_prompt 全文同梱）。
- head 本体の凍結（`refresh_on_events` 空 = Metabolism まで再 capture しない）は**変えない**。凍結は cache 経済の意図された設計であり、欠けていたのは凍結を補う通知の側。

### 3.4 Beat ロックと関所の配置

- Beat ロックの取得点 = PulseController が Beat を開始する位置。取得直後に execution_ledger の関所（pending flush）を実行する。
- ロックの解放は Beat の記録書き込み完了後（例外・cancel 時も `finally` で解放）。
- 作業セッション（WORKER）の連続 Beat は、Beat 境界ごとにロックを解放・再取得する。会話 Beat が間に挟まれることを設計上許可する（「作業中に話しかけたら応答」の土台）。
- **ユーザー割り込みも Beat 境界で効く**（まはー裁定 2026-07-16）: 実行中の Beat は課金と生成が進んでいる可能性があるため、割り込みで中断せず完了を待つ。cancel 要求は Beat 境界で評価し、次の Beat を開始しない形で効かせる。「実行中の Beat を即時中断して応答させる」明示操作は将来オプションとして残す（本工事では作らない）。

## 4. 不変条件

1. **実行の身分は一度だけ解決する** — Beat 内のどの層も ExecutionContext を読み、persona の可変属性から model/thread/line を再推測しない。
2. **記帳は実 model へ** — anchor・TTL・使用量・token threshold の更新先は、実際に応答した model の Session。呼んでいない model の Session 状態を動かさない。
3. **head は (persona, model) に一つ** — 用途・line で出し分けない（[[feedback_head_fixed_per_persona_model_no_gating]] と同じ根）。
4. **head 操作は常に内容ごと届く** — 操作ラベルではなく render 同一断片を、判定なしで全 Session 窓へ。「head に入ったときに見える内容と寸分たがわず同じ内容」（まはー 2026-07-16）。経路は outbox → 知覚バッファ → tail。
5. **編纂は persona に一度、退役・可視化は model の節目に** — どの model も「自分の anchor 以前は編纂済み」が常に成立する。
6. **thread は実行の属性** — persona のグローバル可変状態ではない。Beat が終われば必ず親 thread へ戻る。

## 5. 責任分界

| 項目 | 決める者 |
|---|---|
| ExecutionContext の解決（aspect→model 導出含む） | **基盤**（Beat 開始点で一度） |
| head のキー・凍結・再 capture のタイミング | **基盤**（(persona, model) + Metabolism 時のみ） |
| 通知要否 | **判定なし・常に通知**（まはー裁定。基盤は配送だけを保証する） |
| 通知の本文 | **各 section**（render と同一関数から。基盤は運ぶだけ） |
| Chronicle をいつ・どの範囲で編纂するか | **基盤**（未編纂範囲 + 実行台帳の冪等 claim） |
| どの model の窓をいつ縮めるか | **基盤**（各 model の閾値。ペルソナの意志は関与しない） |

## 6. 移行手順（execution_ledger の Phase 0〜1 と交互に進む）

1. **ExecutionContext 導入**: 型と解決点を作り、既存の暗黙推測箇所（LLM 選択・anchor touch・使用量記帳・history 読み書き）を器経由に置換。この段階では挙動不変（解決結果は従来と同じ値）。
2. **Beat ロック + 関所**（execution_ledger Phase 0 と同時）: main/META 並行 submit の解体を含む。
3. **head/anchor/TTL/last_notified の (persona, model) キー化 + migration**。
4. **head 操作の内容型通知**（issue の解消。META の生きる目的・コア記憶・机から）。
5. **Metabolism 二層分離**（実行台帳の冪等 claim と接続。S2/M1 を同時に閉じる）。
6. **thread の ExecutionContext 化**（Stelis push/pop、S4）。
7. `session.md` / `dynamic_state_sync.md` の正典改訂を同じ wave で行う。

## 7. 引き受ける歪み

1. **META 判断のレイテンシ** — main と並行できなくなるため、会話の Beat が続く間はメタ判断が Beat 境界を待つ（最大 1 Beat）。判断の即時性より記憶の一直線性を優先する（まはー裁定）。
2. **通知の重複ノイズ** — 常時通知のため、操作した本人の窓にも通知が届く（main line で撃った直後は spell 結果の生ログと二重に見える）。判定ロジックの複雑さより、欠落の無さと単純さを取った（まはー裁定）。
3. **migration の一回コスト** — head snapshot / anchor の物理キー変更。既存データは MODEL_KEY 記録から機械的に移行できる見込み。
4. **lightweight Session の新規コスト** — いままで「存在しなかった」lightweight 側の head capture・anchor 管理が実際に走るようになる。capture は Metabolism 時のみなので増分は小さい見込みだが、Phase 実装時に計測する。
5. **割り込みの即時性** — 実行中の Beat は割り込みでも完了まで待つ（課金と生成の保護）。体感の即応性は「次の Beat を開始しない」ことで確保し、即時中断は将来の明示操作に譲る。

## 8. 未確定・レビュー待ち

なし（v0.2 で全点解決）。

解決済み（v0.2、まはーレビュー 1 巡 2026-07-16）:

1. ~~ストリーミング応答中の割り込みと Beat ロック~~ → 実行中の Beat は完了を待つ（お金をかけて動いている可能性があるため）。cancel は Beat 境界で評価。「中断してすぐ応答させる」操作は将来オプション（§3.4）
2. ~~通知要否判定の単純化~~ → **判定なし・常に通知**。ペルソナ体験（自分の操作の通知が届く）を判定で対策しきれない以上、無理に効率化しない。操作起点の push 型へ（§3.3）
3. ~~lightweight Session の Metabolism 閾値~~ → 普通にモデル依存。実行 model のモデル設定から導出（§3.2）
4. ~~memory_weave / chronicle_index の diff 未整理~~ → 本工事に含める。「後に残してもあんま良いこと無い。結局全部やる作業なんだから分かってる時点で潰す」（§3.3）
