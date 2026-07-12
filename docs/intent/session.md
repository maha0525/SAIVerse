# Intent Document: Session

**ステータス**: 起草中 (v0.1, 2026-05-28)。**2026-07-13: [life.md](life.md) が本書 §6 未確定事項に回答**——ライフ（活動区間の宣言）＝制御プレーン / Session＝データプレーンの関係で位置づけ直し、life.md レビュー通過後に本書を吸収改訂する。均等モードではライフ終端が Session 終了の第一基準になる（安全弁基準は存置）。
**位置付け**: Metabolism / head 安定化 / Chronicle 生成 / prompt cache TTL / anchor 管理 を「セッション」という単一概念に統合し、世代の取り残しを解消する。
**前提**: [`cached_head_architecture.md`](cached_head_architecture.md) (head 安定性) / [`line_tag_responsibility.md §10`](persona_cognition/line_tag_responsibility.md) (aspect 統合の手本) / [`cache_lifecycle_control.md`](cache_lifecycle_control.md) (TTL 戦略)

---

## 1. なぜこの整理が必要か

認知モデル (aspect) や head 安定化 (cached_head_architecture) は世代を重ねて進化してきた。しかし周辺機構 — Metabolism、Chronicle 生成、prompt cache の TTL 戦略、anchor 管理 — はそれぞれ独立に古い前提のまま運用されている。

ある機構が更新されても、周辺機構の前提が連動して更新されない。結果として各機構は局所では正しく動くが、組み合わせると整合が取れない状態が累積している。

aspect は model / scope / line_role の 3 概念を「ペルソナの状態の一側面」という単一概念に畳んだ統合の成功例である。本整理はこれと同性質の作業を、散らかった側にも進めることが目的。ただし aspect を中心に据えない (中心を固定するとその射程外の関心事が思考から落ちる)。複数の不整合源を並列に扱いながら統合の機会を見出す。

---

## 2. セッションの定義

### 2.1 セッションとは

**セッション = 節目と節目の間**。ペルソナが活動している連続区間であり、その間 head が安定し、prompt cache が継続して効くことが期待される単位。

### 2.2 開始と終了

| 境界 | 条件 |
|---|---|
| **開始** | 進行中のセッションが無い状態でペルソナが動くとき。自律行動でもユーザーからの話しかけでも区別しない |
| **終了** | それ以上 cache を続けられなくなりそうなとき |

「cache を続けられなくなりそう」 の具体的な判定基準 (件数 / TTL / context 使用率 / explicit hit の有無) は §6 未確定事項。

### 2.3 粒度

セッションの粒度は **(persona, model)**。同じペルソナでも model が違えば別セッション。同じ persona × model なら同じセッション。

「ペルソナ単位で 1 セッション」 を貫きたい願望は議論の過程で出たが、Chronicle 化が一方のモデルに波及しない (= Gemini が生ログを持ち続けているなら Memory Weave 取り込みが不要) ことから、節目はモデル単位にしかなり得ないと判断した。ペルソナの認知一貫性は履歴と Chronicle の永続化で担保され、セッションが束ねるものではない (§5)。

### 2.4 WORKER は親に閉じる子セッション

aspect = WORKER (サブライン) は親 (CONVERSATION / AUTONOMOUS) の中で発生する **子セッション**。

- WORKER 内で context 超過が起きたら、子セッション内だけで Metabolism して継続
- WORKER 終了時、親 head は触らない → 親は WORKER 呼ぶ前と完全に同じ状態
- 親に渡るのは aspect v0.2 既存の `report_to_parent` だけ

WORKER は構造上サブライン (別 line_id) なので、親と独立した head / cache を自然に持つ。

---

## 3. セッションが統合する概念

### 3.1 Metabolism と head 安定化の一体化

これまで Metabolism (節目で履歴を更新する機構) と head 安定化 (節目間で head を変えない機構) は別の機構として語られてきたが、両者は **同じことに向かっている**:

- 節目を作ること
- 節目と節目の間で変わらないようにすること

この 2 つは互いに前提し合う一体の概念。「節目がある」 ことを意味するのが「節目間の不変区間がある」 ことと同じ。両者を **セッション** という単一概念に畳む。

| 旧概念 | セッションでの位置付け |
|---|---|
| Metabolism (発火) | セッションの終了 + 次セッションの開始 |
| head 安定化 | セッション中の不変性保証 |
| anchor (METABOLISM_ANCHORS) | セッションの起点を指すマーカー |
| cache TTL | セッションの寿命の上限値 |

### 3.2 Chronicle 化と履歴縮小の分離

Metabolism は現状「Chronicle 化」と「履歴縮小」を同時に行っているが、両者は本来別の関心事である:

| 関心事 | 性質 | 粒度 |
|---|---|---|
| **Chronicle 化** | 古い履歴を要約として永続保存する | **ペルソナ単位** (履歴の流れに沿ってインクリメンタル成長、モデル非依存) |
| **履歴縮小** | そのモデルの context に乗せる範囲を狭める | **モデル単位** (context 上限がモデル別だから) |

Chronicle は一度作れば全モデルが共有できる。履歴縮小はそのモデルの事情でだけ起きる。両者を分離することで、片方のモデルが context 上限に達しても他方を巻き込まない。

### 3.3 Memory Weave context の再定義

**Memory Weave context = そのモデルの生ログでカバーできない過去を補完するもの**。

- Claude (短い context): 直近 N トークンの生ログ + 見えない過去を Memory Weave で補完 (Chronicle 参照)
- Gemini (長い context): 全生ログがまだ見えてる → Memory Weave 不要 (空 or 最小)

これにより、Chronicle 化が走っても **生ログでまだ過去を見られているモデルの head は変わらない** ことが保証される。Memory Weave を起動するか否かは各モデルが自分の context 上限に応じて独立に判断する。

### 3.4 多モデル並走時の挙動

Claude (メタ判断, 50 分間隔) + Gemini (自律行動, 5 分間隔) のような並走シナリオで:

- 各セッション (Claude セッション / Gemini セッション) は独立に節目を持つ
- 片方のセッションで節目が立っても、もう片方のセッションには波及しない
- Chronicle はペルソナ単位で永続化されるので、後から相手のセッションが節目を迎えたときに Memory Weave 経由で参照可能

これによりまはーが懸念していた「Gemini の頻繁な節目で Claude の cache が壊される」現象は構造的に起きない。

---

## 4. aspect とセッションの関係

aspect (v0.2: `line_tag_responsibility.md §10`) は (line_role, scope, model_tier) を畳んだ分類で、CONVERSATION / WORKER / AUTONOMOUS / META の 4 種。

セッション粒度との関係は以下:

| aspect | line_id | セッションへの影響 |
|---|---|---|
| CONVERSATION | "main" | メインラインで動く、同 model なら同 head 共有 |
| AUTONOMOUS | "main" | 同上 |
| META | "main" | 同上 |
| WORKER | サブライン (別 line_id) | 親に閉じる子セッション (§2.4) |

メインラインで動く 3 つの aspect (CONVERSATION / AUTONOMOUS / META) は line_id を共有するため、同じ (persona, model) を使う限り同じ head を共有し、同じセッションに属する。aspect そのものはセッション粒度に影響しない。

WORKER だけが構造上サブラインなので、必然的に子セッションを形成する。

aspect は「ペルソナの状態の一側面」を表す呼び出し時の分類であり、セッションは「その活動が連続する区間」を表す時間概念で、両者は直交する。

---

## 5. 不変条件への影響

ペルソナ認知モデルの不変条件 (`persona_cognition/README.md`) のうち、本整理で扱いが明確になるもの:

### C-2 単一主体の記憶

ペルソナの認知一貫性 (= 「同じペルソナと話している」 連続感) は **履歴と Chronicle のペルソナ単位の永続化** で担保される。セッションが束ねるのではない。

複数のセッション (Claude セッション / Gemini セッション 等) が並走しても、両者が同じ履歴と Chronicle を参照する限り、ペルソナとしては単一主体である。

### C-7 キャッシュヒット継続を最優先

セッション中は head が安定する (cached_head_architecture が保証) → cache hit が継続。セッションの境界 (= 節目) だけが head を変える明示的な瞬間で、それ以外では head は不変。

aspect 統合と head 安定化の組み合わせで、cache hit 継続性が構造的に保証される。

### C-11 メタ判断はペルソナの自分の思考

META aspect の scope 変動 (試行ターン discardable → 確定ターン committed) は既存の aspect v0.2 ランタイム挙動に内包されており、セッション概念は META の発火条件には関与しない。

---

## 6. 未確定事項

本整理の段階で詰めきれていない論点。次の議論セッションで順に確認する:

### 6.1 「cache を続けられない」 の判定基準

セッション終了条件としてまはー提案 (議論メモ):

- ① セッション開始時のメッセージ数 (Metabolism 後の keep_count、例: 40)
- ② implicit cache が必ず切れる時間 (provider 別。Anthropic=0, OpenAI=要確認, Gemini=24h)。explicit cache 効果中は無効化
- ③ pulse 完了後の context 使用率閾値 (例: 80%、`prompt_token_count / context_length`)。モデル別に設定可能
- 例外: context 長超過エラーが返ったら強制 Metabolism + 再送

これらをどう組み合わせて判定するか、また per-model anchor 構造との整合は未決。

### 6.2 セッション境界での実行内容

セッション終了 → 次セッション開始の繋ぎ目で何が起きるか:

- Chronicle 化 (どの範囲を Chronicle にするか)
- 次セッションの head 再 capture (どの section を新規 capture するか)
- anchor 更新 (新セッションの起点)
- visual context 等の dynamic state 更新

これらの順序と責任分担。

### 6.3 anchor の per-model 構造との整合

現状の `METABOLISM_ANCHORS` は per-model dict。セッション = (persona, model) 粒度と整合するが、 `_resolve_metabolism_anchor` の 3-level fallback (self / other / minimal) はセッション概念の中でどう位置付けられるか。

### 6.4 トリガータイミング

Metabolism (= セッション終了処理) を pre-response で走らせるか post-response で走らせるか。現状は件数 trigger が post-response、TTL trigger が pre-response (Case 3 minimal load) で混在。post-response 統一の方向で議論したが未確定。

### 6.5 ユーザー視点での見せ方

実装が固まってから決める。何も見せなくても悪影響がないなら見せないのが一番分かりやすい、というのが議論で出た方針。

---

## 7. 関連ドキュメント

- [`cached_head_architecture.md`](cached_head_architecture.md) — head 安定性 (本整理の前提)
- [`cache_lifecycle_control.md`](cache_lifecycle_control.md) — Anthropic / Gemini explicit cache の TTL 戦略
- [`line_tag_responsibility.md`](persona_cognition/line_tag_responsibility.md) — aspect 統合の手本 (§10)
- `persona_cognition/README.md` — 不変条件

---

## 改訂履歴

- v0.1 (2026-05-28): 起草。Metabolism / head 安定化 / Chronicle / cache TTL / anchor を「セッション」概念に統合する方向性を整理。aspect の手本を参照しつつ、中心を固定しない方針で統廃合を進める。多モデル並走時の挙動 (Chronicle 化と履歴縮小の分離、Memory Weave 再定義) と WORKER の子セッション扱いを確定。判定基準・トリガータイミング・anchor 整合は §6 未確定事項として持ち越し。
