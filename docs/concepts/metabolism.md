# Metabolism / Anchor（節目）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §6](../overview/landscape.md)、**設計意図**は intent [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md) / [`cached_head_architecture.md`](../intent/cached_head_architecture.md) を参照。

## 一言で

[Session](session.md)（短期記憶）が継続不能になると発火する節目のイベントが **Metabolism**、その起点を指すマーカーが **Anchor**。

## 役割

Metabolism は短期記憶を区切り直す節目であり、同時に**短期記憶と長期記憶をつなぐ**。ここで短期記憶（[head](session.md) snapshot）を再構築しつつ、長期記憶への結晶化（[Chronicle](chronicle.md) 圧縮・[Memopedia](memopedia.md) Fragment 生成）を束ねて実行し、新しい Session を開始する。

## Metabolism（短期リフレッシュ + 長期結晶化）

### 発火条件

**Session が継続不能になる** = cache TTL 切れ（Anchor 判定）、context 過剰など。

### ⚠️ 実行点は2つある（片方は grep で見落とされ続けた）

| 実行点 | 場所 | 発火条件 | 特徴 |
|---|---|---|---|
| **応答後** | `sea/runtime.py` run_meta_user 末尾 → `_maybe_run_metabolism` → `_run_metabolism` | watermark 超過 / トークン閾値（`_metabolism_token_triggered`） | アンカー更新を伴う正規の eviction はここだけ |
| **会話前（pre-response）** | `sea/runtime_context.py` 履歴取得 Case 3 | anchor が**全モデル失効**（TTL 切れ） | コンテキスト構築中に `_generate_chronicle` / `_generate_track_chronicle` を**直接**呼び、low watermark の最小履歴で会話を開始する |

会話前経路は `_maybe_run_metabolism` を経由しないため、実行点を `_maybe_run_metabolism` の呼び出し元 grep で探すと**漏れる**（過去に複数回「会話前経路は無い」と誤答された経緯がある。調べるときは `_generate_chronicle` の呼び出し元まで grep すること）。また、この最小ロードはアンカー更新型の eviction を通らない**サイレント eviction** であり、旧ウィンドウは Chronicle 化はされるがそれ以外の節目処理を受けない。keepalive 連鎖は Active の間しか繋がらないため、Idle/Sleep 落ち・夜間・再起動を挟んだ最初の会話はほぼ確実に会話前経路を踏む（= 日常的なイベント）。

設計上の含意と将来の扱いは intent [`gold_panning.md`](../intent/gold_panning.md) §3.1 / §3.6 を参照。

### 発火時にやること

1. 全 Section に `capture(live_state)` を走らせて**短期記憶（head snapshot）を再構築**
2. 同時に**長期記憶への結晶化**（履歴圧縮・Chronicle 化・Fragment 生成）を束ねて実行
3. **新しい Session を開始する**

`_resolve_metabolism_anchor` が3段フォールバック（当該モデルの anchor → 別モデルの最新 → 最小ロード）で文脈取得を切り替える。**実装済**。

> ⚠️ **短期 → 長期の選別（要整理）**: 短期記憶に流入する情報がすべて長期記憶に残るべきとは限らない。特に**システム通知**（入室・アイテム増減など）は「その場で分かればいい」情報。現状は Chronicle 生成時に除外しているが、そもそも長期記憶（生ログ）側に渡さない入口選別の方が綺麗（→ [issue](../issues/short_term_to_long_term_memory_filtering.md)）。

## Anchor（節目のマーカー）

Metabolism の起点を指すマーカー。

- `METABOLISM_ANCHORS` は **per-model dict** として persona に紐付き、各 model ごとに `{anchor_id, updated_at, ttl_seconds}` を持つ
- `updated_at` は prompt cache write 時刻で、LLM コール後に `_touch_anchor_after_llm_call` で touch される
- `anchor_updated_at + ttl < now` で TTL 切れ（= Session 継続不能の予兆）と判定 → 次の context 構築時に Metabolism が自動 trigger される

**実装済**。

## 実装

- 発火・アンカー解決: `sea/runtime.py` / `sea/runtime_context.py`（`_resolve_metabolism_anchor` / `_touch_anchor_after_llm_call`）
- head 再構築: `sea/head_pipeline/integration.py`
- 結晶化: `sai_memory/arasuji/generator.py`（`ArasujiGenerator` + `entity_extractor` の相乗り）
- Anchor 状態: persona の `METABOLISM_ANCHORS`（per-model dict）

## 関連概念

- [Session](session.md) — Metabolism が区切り直す対象
- [head](session.md) — Metabolism が再構築する安定領域
- [Chronicle](chronicle.md) / [Memopedia](memopedia.md) — 長期結晶化の中身
- [line / aspect](line.md) — scope が結晶化対象かどうかに効く

## 参照

- intent: [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md)
- 地図: [`landscape.md`](../overview/landscape.md) §6
