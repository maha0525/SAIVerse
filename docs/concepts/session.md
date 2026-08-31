# Session / head（短期記憶）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §6](../overview/landscape.md)、**設計意図**は intent [`session.md`](../intent/session.md) / [`cached_head_architecture.md`](../intent/cached_head_architecture.md) を参照。

## 一言で

ペルソナが「今見ているもの」全体が **Session**（短期記憶）、そのうち prompt cache が継続して効く安定した先頭領域が **head**。

## 役割

[長期記憶（SAIMemory）](saimemory.md)が蓄積された経験の全体だとすれば、短期記憶はそこから引き出された・今まさに進行中の・外界から届いたばかりの情報の作業領域（ワーキングメモリ）。**すべての LLM 判断（[Meta-Judgment](meta-judgment.md) / [Beat](beat.md) 生成）に供給される**。

## Session（短期記憶）

### 流入するもの

| 流入する情報 | 出どころ |
|---|---|
| 生ログの末尾 | Thread（長期記憶 §5）から最近の Message を引き出し |
| head | キャッシュの効く安定領域（head ⊂ Session） |
| 現 Pulse 内の各 [Beat](beat.md) | Spell 結果込み（`memory_recall` で引いた長期記憶もここに乗る） |
| [Building](building-city.md) の未読メッセージ | 外界からの新着入力 |
| システム通知 | 入室・退室・アイテム増減などの状態変化 |

### 粒度

`(persona_id, model_key)` 単位。同じペルソナでも model が違えば別 Session。WORKER（サブライン）は親 Session の中の「子 Session」。複数 model 並走（Claude メタ判断 + Gemini 自律など）では各 Session が独立に [Metabolism](metabolism.md) を発火する。

> **起草中**（`session.md` v0.1）。**コード上にはまだ「Session」という統一制御単位は存在しない**（現状は anchor touch → 履歴取得 → head render の三部構成で個別に動く）。旧 `working_memory` テーブルによるワーキングメモリ実装は死んでおり、短期記憶は Session 概念へ統合される方向。

## head（短期記憶の安定部分）

prompt cache が継続して効く先頭領域。`LineHeadSnapshot` として freeze された Section 群（`common_prompt` / `persona_self` / `building` / `spell_list` / `available_playbooks` 等）の render 結果で構成される。

- snapshot の更新は [Metabolism](metabolism.md) または明示的イベントでのみ起き、平時は immutable
- head 文字列が変動しない限り cache hit が継続する
- **機構は実装済**（`sea/head_pipeline/`、Phase 1 完成）

> **head 設計の要点**: head は `(persona, model)` 固定。用途・ラインで出し分けると prefix キャッシュが壊れる。恒常セクションは条件分岐なしで固定追加する。

## 実装

- head パイプライン: `sea/head_pipeline/`（`pipeline.py` / `sections/` / `store.py` / `types.py` / `integration.py`）
- Session 制御: 統一制御は未実装（`sea/runtime.py` / `sea/runtime_llm.py` に分散）

## 関連概念

- [SAIMemory](saimemory.md) — Session に末尾を供給する長期記憶
- [Metabolism](metabolism.md) — Session を区切り直す節目
- [Meta-Judgment](meta-judgment.md) / [Beat](beat.md) — Session を判断材料にする
- [line / aspect](line.md) — scope が Session への残り方を決める

## 参照

- intent: [`session.md`](../intent/session.md) / [`cached_head_architecture.md`](../intent/cached_head_architecture.md)
- 地図: [`landscape.md`](../overview/landscape.md) §6
