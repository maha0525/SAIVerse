# Beat（ペルソナの最小行動単位）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §4](../overview/landscape.md) を参照。

## 一言で

ペルソナが「ひと区切りの行動を表出する」最小単位。喋ること・道具を使うこと・自律的に内省を表出すること、すべてが「1 Beat」。

## 役割

[Pulse](pulse.md)（認知サイクル）より小さく、message（記録単位）とも一致しない中間単位。応答(reply)でも発話(utterance)でもない中立語で、脚本術の「beat = キャラクターの最小の行動・意図単位」に由来する。1回の Pulse は1つ以上の Beat を生む。

## 仕組み

### Beat の構成

発話ノード(LLM)の出力 + [Spell](spell.md) loop 全 round の本文 + 各 Spell の `<user_only>` 結果ブロック + 最終 continuation の連結。

### 記録先で2つに割れる

| 用途 | 実体 | 内容 |
|---|---|---|
| **表示用** | `full_merged_text` | Spell 結果込みの合成版 |
| **長期記憶保存用** | `final_continuation` | 最終発言のみ（重複回避） |

- **表示用**の Beat は [Building](building-city.md)（共有メッセージ場）に積まれてユーザーや他ペルソナに感知される
- 同時に**自分の [Session](session.md)（短期記憶）にも積まれ**、次の Beat や [Meta-Judgment](meta-judgment.md) の文脈になる

## 実装ギャップ（重要）

> ⚠️ Beat は概念として確立・命名されたが、**実装には型 / クラスとして存在しない**。実体は `sea/runtime_llm.py` の `_run_spell_loop` の戻り値 `full_merged_text`（ただの `str`）でしかない。名前が無いまま実装が育ったため、概念図で `Playbook → Spell` と中間が飛ばされる歪みを生んでいた。将来 `Beat` を型として導入するリファクタが必要（→ [issue](../issues/beat_concept_not_typed_in_implementation.md)）。

## 実装

- 生成: `sea/runtime_llm.py`（`_run_spell_loop` → `full_merged_text` / `final_continuation`）
- 記録: 表示用は [Building](building-city.md) 履歴へ、保存用は [SAIMemory](saimemory.md) の `messages` へ

## 関連概念

- [Pulse](pulse.md) — Beat を内包する上位単位
- [Playbook](playbook.md) — 発話ノードの LLM 出力が Beat になる
- [Spell](spell.md) — Beat 内の平文から発動する
- [Session](session.md) / [Building](building-city.md) — Beat が積まれる先

## 参照

- 地図: [`landscape.md`](../overview/landscape.md) §4
- issue: [`beat_concept_not_typed_in_implementation.md`](../issues/beat_concept_not_typed_in_implementation.md)
