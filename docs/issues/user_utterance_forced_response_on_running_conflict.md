# Issue: ユーザー発話の強制応答 — running Track 衝突時の扱い

**ステータス**: 🔲 未着手 (設計検討待ち — 自律行動v2 と合わせて考える)
**優先度**: medium
**作成日**: 2026-07-07
**関連**: `docs/intent/persona_cognition/pulse_dispatch.md` §4.2、`saiverse/track_handlers/user_conversation_handler.py` `on_user_utterance`、`saiverse/meta_layer.py`

## 背景

2026-07-07 の無応答バグ (Idle ペルソナに話しかけたら `meta_judgment_life_purpose` が alert を横取りし、life_purpose 設定だけ行われて返事が返らなかった) を受けて、ユーザー発話の応答経路を2段階で修正した:

1. **止血**: `_classify_situation` で `alert_present` を `life_purpose_unset` より優先に変更
2. **本命**: `on_user_utterance` の熟慮経路を「**別の running Track が存在する場合のみ**メタ判断」に変更。衝突がなければメタ判断を経由せず直接 activate + メインライン応答 (pulse_dispatch.md §4.2 まはー判断 Q2 の改訂)

これにより Idle / pending 状態のペルソナへの呼びかけは常に即応答になった。

## 残る論点: running 衝突時も強制応答に寄せるか

現状、別 Track (作業セッション等) が running 中の割り込みだけはメタ判断が仲裁しており、メタ判断が activate を選ばなければ**依然として無応答**になりうる。

まはーの問題提起 (2026-07-07):

> Activeであってもユーザーの呼びかけに対して一回LLM通して対応検討するの無駄なんじゃないか。強制的に返事させていいのでは。無視する選択肢は返事の中で使えるspellを用意して対応することも可能。

### 強制応答案の利点

- ユーザーの呼びかけが構造的に飲まれる経路が完全に消える
- メタ判断LLM + メインラインLLM の2段が1段になる (メタ判断は別コンテキストなのでキャッシュ的にもメインライン直行が得)
- 「無視する / 今手が離せない」は、返事の場 (メインライン) でペルソナ自身の文脈と口調で表現できる — メタ判断の裏側で黙殺するより表現力が上がる

### 設計が必要な点

- **preempt された running 作業セッションの扱い**: 強制応答で問答無用に中断するのか、返事の中のスペル (例: 「作業を続ける」「会話に切り替える」「後で戻る」) で選ばせるのか
- スペルの具体形: 会話を閉じて元の Track に戻る / running Track を pause する等、Track 操作系スペルの整備
- 自律行動v2 の骨組み再設計 (暮らし/仕事分離 + 予算付き作業セッション + アーティファクト) と密接に絡む — 作業セッションの中断・再開のセマンティクスはそちらで定義されるべきもの

## 方針

単独では進めず、自律行動v2 の intent doc 起草時に「割り込みと復帰」(Phase 5 UC-2 系統) の一部としてまとめて設計する。

## 関連リソース

- `docs/intent/persona_cognition/pulse_dispatch.md` §4.2 (Q2 改訂の経緯)
- `docs/intent/persona_cognition/phases/phase_5_autonomy.md` (UC-2 割り込みと復帰)
- memory: `project_autonomous_behavior_v2_redesign`、`project_persona_cognition_phase5_uc2`
