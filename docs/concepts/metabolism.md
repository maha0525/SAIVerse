# Metabolism / Anchor（節目）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §6](../overview/landscape.md)、**設計意図**は intent [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md) / [`cached_head_architecture.md`](../intent/cached_head_architecture.md) を参照。

## 一言で

[Session](session.md)（短期記憶）が継続不能になると発火する節目のイベントが **Metabolism**、その起点を指すマーカーが **Anchor**。

## 役割

Metabolism は短期記憶を区切り直す節目であり、同時に**短期記憶と長期記憶をつなぐ**。ここで短期記憶（[head](session.md) snapshot）を再構築しつつ、長期記憶への結晶化（[Chronicle](chronicle.md) 圧縮・[Memopedia](memopedia.md) Fragment 生成）を束ねて実行し、新しい Session を開始する。

## Metabolism（短期リフレッシュ + 長期結晶化）

### 発火条件

**Session が継続不能になる** = cache TTL 切れ（Anchor 判定）、context 過剰など。

**context 過剰の判定は文字数の二数** = 上限と残す量（2026-07-28、intent [`arasuji_levels.md`](../intent/arasuji_levels.md) §9）。旧「モデルごとのメッセージ数」は 2026-07-25 に単位ごと廃止（三水位へ）、三水位の低水位は 2026-07-28 に未使用化（残す量が保護を兼ねる。設定キーは掃除まで残置 — intent §12-8）。

| 数 | 意味 | 一律既定 | モデル設定キー |
|---|---|---|---|
| 残す量 | 畳んだ後に残す直近の文字数 = 保護範囲。上限との差がバッファで、発火は「たまに・まとめて」になる（キャッシュ保護） | 60,000 | `metabolism_target_chars` |
| 上限 | これを超えたら発火（未設定 = null なら `token_triggered` のみで発火） | 120,000 | `metabolism_high_chars` |
| (低水位) | 旧三水位の名残り。**未使用** | 40,000 | `metabolism_low_chars` |

数え方は**提示される提示コンテキストの文字数**（圧縮区間は digest に置き換わった後の量）。グローバル上書きは `POST /api/config/metabolism`（低 ≤ 目標 ≤ 高 を入口で検証）。

### ⚠️ 実行点は2つある（片方は grep で見落とされ続けた）

| 実行点 | 場所 | 発火条件 | 特徴 |
|---|---|---|---|
| **応答後** | `sea/runtime.py` run_meta_user 末尾 → `_maybe_run_metabolism` → `_run_metabolism` | watermark 超過 / トークン閾値（`_metabolism_token_triggered`） | アンカー更新を伴う正規の eviction はここだけ |
| **会話前（pre-response）** | `sea/runtime_context.py` 履歴取得 Case 3 | anchor が**全モデル失効**（TTL 切れ） | コンテキスト構築中に `generate_chronicle` を**直接**呼び、low watermark の最小履歴で会話を開始する |

会話前経路は `_maybe_run_metabolism` を経由しないため、実行点を `_maybe_run_metabolism` の呼び出し元 grep で探すと**漏れる**（過去に複数回「会話前経路は無い」と誤答された経緯がある。調べるときは `generate_chronicle` の呼び出し元まで grep すること）。また、この最小ロードはアンカー更新型の eviction を通らない**サイレント eviction** であり、旧ウィンドウは Chronicle 化はされるがそれ以外の節目処理を受けない。keepalive 連鎖は Active の間しか繋がらないため、Idle/Sleep 落ち・夜間・再起動を挟んだ最初の会話はほぼ確実に会話前経路を踏む（= 日常的なイベント）。

設計上の含意と将来の扱いは intent [`gold_panning.md`](../intent/gold_panning.md) §3.1 / §3.6 を参照。

### 発火時にやること

1. 全 Section に `capture(live_state)` を走らせて**短期記憶（head snapshot）を再構築**
2. 同時に**長期記憶への結晶化**（履歴圧縮・Chronicle 化・Fragment 生成）を束ねて実行
3. **新しい Session を開始する**

`_resolve_metabolism_anchor` が3段フォールバック（当該モデルの anchor → 別モデルの最新 → 最小ロード）で文脈取得を切り替える。**実装済**。

> ⚠️ **短期 → 長期の選別（要整理）**: 短期記憶に流入する情報がすべて長期記憶に残るべきとは限らない。特に**システム通知**（入室・アイテム増減など）は「その場で分かればいい」情報。現状は Chronicle 生成時に除外しているが、そもそも長期記憶（生ログ）側に渡さない入口選別の方が綺麗（→ [issue](../issues/short_term_to_long_term_memory_filtering.md)）。

## Anchor（節目のマーカー）

Metabolism の起点を指すマーカー。

- anchor は **`session_anchor` テーブル**（1 行 = 1 (persona, model)、列 = `ANCHOR_MESSAGE_ID / TTL_SECONDS / UPDATED_AT`）に持つ（§6-3a、2026-07-17。旧 `AI.METABOLISM_ANCHORS` 単一 JSON 列は backfill の変換元としてのみ残存）
- `UPDATED_AT` は prompt cache write 時刻で、LLM コール成功後に `touch_anchor_after_llm_call` で touch される。記帳先は **usage.model（実際に応答した model）**、touch する anchor は prefix 組成時の値を `state["_prefix_anchor_id"]` で call-local に運ぶ（persona 属性経由は廃止 — §6-5）
- `UPDATED_AT + ttl < now` で TTL 切れ（= Session 継続不能の予兆）と判定 → 次の context 構築時に Metabolism が自動 trigger される
- **二層分離（§6-5、2026-07-17）**: 編纂（Chronicle 生成）は persona に一度（実行台帳の冪等 claim `metabolism.run`）、退役（anchor 前進）は model ごと。**退役は編纂の成功（status ok / disabled）でゲート**され、編纂失敗時は据え置き → 次回自然再試行（S2 根治）

**実装済**。

## 実装

- 発火・アンカー解決: `sea/runtime.py` / `sea/runtime_context.py` / `sea/session_lifecycle.py`（`resolve_metabolism_anchor` / `touch_anchor_after_llm_call` / `maybe_run_metabolism`）
- head 再構築: `sea/head_pipeline/integration.py`（可視化は anchor を進めた model の (persona, model) snapshot のみ — §6-5）
- 結晶化 (W4 で episode 整列に世代交代): `sai_memory/arasuji/alignment.py`（整列計画）+ `executor.py`（チャンク実行）+ `bands.py`（列のあふれ束ね）+ `entity_extractor` の相乗り。冪等 claim は実行台帳（`saiverse/execution_ledger.py`）。詳細は [Chronicle](chronicle.md)
- 退場の計画: `sea/eviction_plan.py`（純関数 `plan_eviction` — 残す量より古い側を、古い順に U ずつ刻んで全部畳む。切り位置は pulse 関節に寄せる。**エピソードに畳みを止める権利は無く**、末尾の U 未満の端数は次回へ残す。旧 episode 単位・二段構えは 2026-07-28 世代交代 — intent [`arasuji_levels.md`](../intent/arasuji_levels.md) §4）
- 提示コンテキストの圧縮区間と提示: `sea/session_window.py`（`SessionWindow` = anchor + 生ログ + 提示、`apply_folds` が digest 置き換え）。圧縮区間は `session_anchor.FOLDED_RANGES_JSON` に (persona, model) 単位で持つ
- 編纂範囲: 「今回退場させる範囲そのもの」（`generate_chronicle(compile_groups=...)`）。退場する集合と編纂する集合を一致させることが、下限「退場したものは必ず編纂されている」の手続き上の保証
- Anchor 状態: `session_anchor` テーブル（1 行 = 1 (persona, model)）

## 関連概念

- [Session](session.md) — Metabolism が区切り直す対象
- [head](session.md) — Metabolism が再構築する安定領域
- [Chronicle](chronicle.md) / [Memopedia](memopedia.md) — 長期結晶化の中身
- [line / aspect](line.md) — scope が結晶化対象かどうかに効く

## 参照

- intent: [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md)
- 地図: [`landscape.md`](../overview/landscape.md) §6
