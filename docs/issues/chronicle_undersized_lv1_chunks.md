# Issue: Chronicle の一次あらすじが標準被覆を大きく下回る (退場の刻み幅に持ち越しの器が無い)

**ステータス**: 🟠 実装中 — 当初の借用実装は撤回、[`../intent/chronicle_eviction.md`](../intent/chronicle_eviction.md) で再設計 → 2026-07-25 に**下段 (生ログ→一次あらすじ) のみ**実装 (`e8c061d`)。**上段 (二次以上のあらすじ) との接続が未設計で、生ログの切れ目を飛び越えた偽の隣接が生まれうる** → 調査・案出しからやり直し（まはー判定 2026-07-25、入口は [走行メモ](../handoff/2026-07-25_chronicle_eviction_handoff.md)）。実機検証は上段が片付いてから

**⚠️ 目的の改訂 (2026-07-25 まはー裁定)**: 本 issue はもともと「U 未満の小粒な一次あらすじを作らない」を目的に掲げていた。これを **「小粒を日常経路で作らない。最後の手段としてのみ許す」** に緩める。

理由: 提示コンテキストの先頭に「まだ終わっていない episode の、U に届かない端数」が居座ると、そこを畳めない限り **anchor が永久に進まない**。小粒を絶対に作らないという縛りは、その詰まりを永続させる。**U に届いたら普通に畳むのに、届かないときだけ生で死守するのは判断基準として一貫していない** — U は「優先度」の材料であって「畳んでいいか」の材料ではない。

歯止めは二つ。(a) U 以上の候補が一つでもあればそちらを常に優先する（[`chronicle_eviction.md`](../intent/chronicle_eviction.md) §5-5 の二段構え）。(b) 二段目は一回の Metabolism で一回だけ。小粒が出た回は `used_undersized_open_fold` でログに残るので、定常運転に堕ちていないか観測できる。

なお 1000字未満の端数は既存の恒等圧縮 (§4-3) に落ちるので **LLM コールは走らない**（無駄な要約をしない）。
**優先度**: high (放置すると毎日ノードが増え続け、次数の意味論が回復不能に崩れる)
**作成日**: 2026-07-24
**関連**: [`../intent/experience_structure.md`](../intent/experience_structure.md) §4-2 / §4-3 / §4-4 / §4-6 ・ W4 (`277bd8d`, 2026-07-21) ・ [`../overview/audit_remediation_plan.md`](../overview/audit_remediation_plan.md) W4 実機検証

## 何が起きているか (実データ)

`eris_city_a` の Chronicle 一次あらすじを W4 前後で並べると、被覆字数が桁で落ちている。

| short_id | 出自 | 被覆字数 | メッセージ数 | 作成 |
|---|---|---|---|---|
| 497 | (W4 前・旧 20 件バッチ) | 15,325 | 20 | 07-21 10:26 |
| 500 | identity | 974 | 1 | 07-22 02:59 |
| 507 | identity | 130 | 1 | 07-22 09:00 |
| 508 | batch | 3,130 | 3 | 07-22 09:15 |
| 511 | identity | 58 | 1 | 07-22 15:36 |
| 512 | batch | 1,177 | 1 | 07-22 15:51 |
| 517 | identity | **9** | 1 | 07-22 19:32 |
| 518 | batch | 3,039 | 4 | 07-22 19:51 |
| 527 | identity | 20 | 1 | 07-23 10:36 |
| 528 | batch | 1,578 | 2 | 07-23 10:51 |
| 534 | batch | 5,408 | 6 | 07-23 23:44 |

- 一次あらすじの標準被覆 **U = 10,000 字** ([`alignment.py:53`](../../sai_memory/arasuji/alignment.py) `DEFAULT_TARGET_CHARS`) に対し、W4 以降の実測は **9 〜 6,344 字**。U に到達したノードは 07-22 以降ゼロ。
- `identity` (恒等圧縮 = 生ログをそのまま一次あらすじに置く) が **19 件**。9 字・20 字・58 字のノードまである。
- **identity(1 件) → 15〜30 分後に batch(数件)** がほぼ交互に並ぶ。例: 517 (9 字, 19:32) の 19 分後に 518 (4 件, 3,039 字, 19:51)。この 2 つは本来 1 つのノードであるべき隣接範囲。

## 不変条件と、それが破れている場所

**不変条件** (experience_structure.md §4-6): 次数 k ノードの標準被覆は `U × B^(k-1)`。一次あらすじ≒ 1 万字、二次あらすじ≒ 10 万字。この幾何級数が「木の深さ = 時間の粗さ」を成立させている。

**所有者**: 一次あらすじの被覆量を決めるのは整列計画 [`sai_memory/arasuji/alignment.py`](../../sai_memory/arasuji/alignment.py) の `plan_alignment` / `_plan_run`。

**破れ方**:

1. 自動経路の編纂は Metabolism ごとに走り、`evict_boundary_epoch` (= 新 anchor の created_at) より前のメッセージだけを対象にする ([`session_lifecycle.py:1157`](../../sea/session_lifecycle.py))。**この退場の刻み幅は U と無関係**で、実際には数件ずつ。
2. `_plan_run` は run の末尾で `_flush_pending()` を**無条件に**呼ぶ ([`alignment.py:307`](../../sai_memory/arasuji/alignment.py))。目標被覆に届いていなくても、そこにあるものを確定する。
3. `min_llm_chars` (1,000 字) 未満で、かつ直前に `CHUNK_LLM_BATCH` が無ければ `CHUNK_IDENTITY` になる ([`alignment.py:270-283`](../../sai_memory/arasuji/alignment.py))。**ここが問題の核心**: §4-3 の恒等圧縮は「**束ねる相手もいない**豆粒」のための逃げ道なのに、実際には「**束ねる相手がまだ退場していない**だけ」の豆粒が同じ扱いを受けている。
4. `executor.execute_plan` はサイズによる門番を持たず、計画されたチャンクを全て確定する。
5. §4-4 (同一レベルの再圧縮禁止) により、**一度一次あらすじとして確定したら、後から来た隣接範囲と束ね直すことは構造的にできない**。取り返しがつかない。

つまり **「まだ材料が揃っていない」と「もう材料は来ない」を区別する器が無い**。整列計画は呼ばれた瞬間のスナップショットしか見ておらず、端数を次回まで持ち越す経路が存在しない。

## 誰に効くか (実害)

- **ペルソナ**: 一次あらすじに「ステーキ焼いたよー」1 行だけのノードが立つ。Chronicle 注入予算 (現状 20,000 字) を、9 字のノードのヘッダが食う。読み出し時の粒度が壊れる。
- **束ね**: 列のあふれ束ね (`bands.py`) は二次あらすじの目標を 100,000 字に置く。一次あらすじが平均 1,000 字だと、二次あらすじの親 1 個が**子 100 個**を飲むことになる。統合プロンプトに 100 個の digest が並ぶ = LLM 入力が肥大し、「一段粗い視点」という次数の意味も失われる。
- **蓄積速度**: 07-22 〜 07-23 の 2 日で一次あらすじが 36 個増えた。旧経路なら 3〜4 個相当。

## 解決案候補

### 案 A: 端数の持ち越し (carry-over) — 本命

`_plan_run` の末尾で、目標未達かつ「run の末尾 = 退場境界に接している」チャンクを**確定せず捨てる**(＝次回の編纂で、次に退場する範囲と一緒に計画し直す)。

- `bands.py` の `_select_bundle_run` が既に同じ思想を持っている: 「壁で打ち切られた目標未達の列は束ねず持ち越す (小粒親を作らない)」。**一次あらすじにだけこの規律が無い**のは非対称。
- 実装は `plan_alignment` に「末尾の端数を持ち越すか」のフラグを足し、自動経路 (`evict_boundary_epoch` あり) では持ち越し、全量整理 (force / session close) では従来どおり吐き切る、が素直。
- 副作用: 退場したのに未編纂のメッセージが一時的に増える。想起経路がそこを引けるかの確認が要る。

### 案 B: 退場の刻み幅を U に合わせる

Metabolism の evict 境界を「U 字分溜まってから」動かす。設計の第 1 原則 (退場時圧縮) を素直に読めばこちらだが、anchor と提示提示コンテキストの設計に手が入るので影響範囲が広い。案 A で足りるなら過剰。

### 案 C: identity の適用条件を絞る

「直前に LLM_BATCH が無い」ではなく「**前後がともに編纂済み**(＝もう束ねる相手が来ない)」を identity の条件にする。案 A と併せると、退場境界に接した端数は持ち越し、本当に孤立した豆粒だけが identity になる。案 A の補完として同時にやる価値が高い。

**推し = 案 A + 案 C**。

## 既に作られてしまったノードの扱い

07-22 以降の小粒一次あらすじ(19 identity + 17 batch) は、§4-4 により後から束ね直せない。選択肢:

1. 放置 (二次あらすじへの束ねで結果的に飲まれるのを待つ) — 一番安全だが二次あらすじの子が 100 個になる問題は残る
2. 該当期間の一次あらすじを削除して再編纂 — `DELETE /api/people/{id}/arasuji/{entry_id}` が子の `is_consolidated` を戻す実装になっているので機構としては可能。ただし**本番ペルソナの記憶の書き換え**なのでまはーの明示承認が要る

修正を入れてから 1 を選ぶか 2 を選ぶかを決める。

## 関連リソース

- [`sai_memory/arasuji/alignment.py`](../../sai_memory/arasuji/alignment.py) — 整列計画 (純関数)。`_plan_run` / `_flush_pending`
- [`sai_memory/arasuji/executor.py`](../../sai_memory/arasuji/executor.py) — チャンク確定。サイズ門番なし
- [`sai_memory/arasuji/bands.py`](../../sai_memory/arasuji/bands.py) `_select_bundle_run` — 二次以上のあらすじ 側の「持ち越し」実装 (参考にすべき先例)
- [`sea/session_lifecycle.py:1067`](../../sea/session_lifecycle.py) `generate_chronicle` — 全編纂入口の合流点。`evict_boundary_epoch`
- 実データ: `~/.saiverse/personas/eris_city_a/memory.db` の `arasuji_entries` (short_id 497 以降)
- ログ: `~/.saiverse/user_data/logs/20260722_023944/backend.log` の `[executor] chunk committed:` 行

## 実装 (2026-07-24) — ⚠️ この借用案は撤回。新設計は [`chronicle_eviction.md`](../intent/chronicle_eviction.md)

**撤回 (2026-07-24)**: 下記の「提示中の生ログを最小限巻き込む」借用案は、Codex レビューで **open episode 分断の P1** が出て行き詰まった (提示中範囲の借用が open episode に及ぶと、後から成長する episode が複数の一次あらすじへ不可逆に分断され、digest の被覆と source_ids が食い違う)。まはーとの対話で、退場の粒度を **episode 単位・文字数水位**に変える上流の解に到達し、そちらを正典 ([`../intent/chronicle_eviction.md`](../intent/chronicle_eviction.md)) とした。前半で入れた借用実装 (alignment.py / session_lifecycle.py / experience_structure §4-1 改訂 / テスト) は撤回予定。以下は経緯として残す。

まはーとの設計対話で、案 A+案 C を**より上流の解に発展させて**採用した。核は**不変条件の反転**:

- 守るのは上限「退場したものだけ編纂」ではなく、下限「**退場したものは必ず編纂されている**」(experience_structure.md §4-1 に明文化)。
- 退場分の端数が U 未満なら、**アンカー以降の提示中の生ログを最小限だけ巻き込んで** U に満たす (努力目標: 巻き込みは最小に。二重化は設計が意図して受容するトレードオフ)。
- **純提示中の末尾は持ち越す** (次の退場と一緒に再計画) = 案 A の持ち越しを、退場分を含む端数だけ確定する形で実現。案 C (identity 条件の厳格化) はこの枠組みに吸収 (identity は両側編纂済みに挟まれた孤島だけ)。

実装:

- [`alignment.py`](../../sai_memory/arasuji/alignment.py) `plan_alignment` に任意引数 `evict_boundary` を追加。指定時、末尾の純提示中チャンク (全メッセージ `created_at >= evict_boundary`) を確定せず落とす (`_carry_pure_presented_tail`)。退場分→提示中の時系列 U 束ねで、境界をまたぐチャンクは U 到達まで = 最小巻き込みが自動成立。`None` (force/estimate 全量経路) は従来どおり全確定 = **後方互換**。
- [`session_lifecycle.py`](../../sea/session_lifecycle.py) `generate_chronicle` の事前フィルタ (退場分だけに絞る) を撤去し、全メッセージ + `evict_boundary_epoch` を planner に渡す。冪等 claim キー `_window_end_id` は確定チャンクの末尾なので持ち越しと整合。
- テスト: `tests/test_arasuji_alignment.py` に `TestEvictBoundaryCarry` 5 件 (持ち越し/最小巻き込み/エッジ確定/全提示中空/None 全確定)。全 25 件緑、関連スイート 115 passed、ruff clean。

**残**: (1) 実機検証 — 実際の Metabolism で一次あらすじが U 級で立つか。(2) 既に作られた小粒一次あらすじ(19 identity + 17 batch) の掃除方針 (放置 or 削除再編纂。後者は本番ペルソナ記憶の書き換えでまはー承認必須) — 未決のまま。

## ログ

- 2026-07-24: 発見・起票。エリスの Chronicle に単独発言のエントリが並んでいるという、まはーの観察から。別件の重複メッセージ調査 ([`chronicle_orphan_duplicate_user_messages.md`](chronicle_orphan_duplicate_user_messages.md)) の途中で判明した
- 2026-07-24: 実装 (上記)。設計対話で案 A+C → 不変条件反転へ発展。純関数 + 呼び出し側 + テスト、experience_structure.md §4-1 改訂同梱
- 2026-07-24 (同日夜): Codex レビューで借用案に open episode 分断の P1 → 借用案ごと撤回、[`chronicle_eviction.md`](../intent/chronicle_eviction.md) (episode 単位・文字数三水位) へ再設計
- 2026-07-25: chronicle_eviction intent まはーレビュー通過・設計確定 (実装待ち)。本 issue の解消は同実装に包含 — 実装着手時に借用実装 (alignment / session_lifecycle / テスト、未コミット) を撤回してから新設計を入れる。experience_structure §4-1 は新設計の文面に書き直し済み。監査工程上は W4 の差し戻し行 ([audit_remediation_plan.md](../overview/audit_remediation_plan.md))
