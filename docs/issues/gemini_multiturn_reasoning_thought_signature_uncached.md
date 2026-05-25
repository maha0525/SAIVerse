# Issue: Gemini マルチターン推論 (thought signature) が cache 不可で毎ターン課金される

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-25
**関連**: `docs/intent/cache_lifecycle_control.md` (Gemini explicit cache / Phase 3)、`llm_clients/gemini.py` (`_store_thought_signature` / thought_signature 再送経路)

## 背景

Gemini explicit cache (Phase 3 / B 戦略) の実機検証中に判明。Gemini 3.5 Flash と対話すると、
入力トークンの一部 (実測 ~10,579 / 66,270) が**キャッシュに乗らず毎ターン全額課金**される。

原因 (実測 + 公式ドキュメントで確定):
- Gemini 3.5 Flash は **thought preservation がデフォルト ON**。過去全ターンの推論コンテキスト
  (`thought_signature` = 暗号化された思考トークン) を会話履歴に保持し、毎リクエストで再送する。
  SAIVerse も思考継続のため過去ターンの `thought_signature` を再送している (実機ログで 18 ターン分確認)。
- **Gemini の context cache は text + media はキャッシュするが、thought_signature はキャッシュしない**
  (公式ドキュメントにキャッシュ可否の記載なし、実測で cache 内訳が text+media のみ = thinking 非キャッシュ)。
- 結果、過去ターンの思考 ~10k トークンが**キャッシュ不可のまま毎ターン課金**される。継続的な自律対話では
  積み上がってコスト影響が大きい。

**これは B 実装のバグではない**。B は prefix (text+media) を正しくキャッシュしており、非キャッシュ分は
Gemini 仕様 (thought signature 非キャッシュ + デフォルト ON の thought preservation) による。

## 解決案候補

公式ドキュメントによれば、thought preservation を無効化する API パラメータは無く、唯一の制御手段は
**「過去ターンの `thought_signature` を送信前に履歴から剥がす (clear)」** こと。

> "If your application performs simple queries or you want to minimize costs in long conversations,
>  you can clear previous thought signatures from the conversation history."
> — https://ai.google.dev/gemini-api/docs/thought-signatures

→ **SAIVerse に「マルチターン推論 ON/OFF」設定を追加**する:
- **ON (現状)**: 過去ターンの thought_signature を再送 → ターン跨ぎの推論継続。ただし ~10k/ターンが非キャッシュ課金
- **OFF**: 送信前に**過去ターン**の thought_signature を剥がす → 非キャッシュ分が消える。代償はターン跨ぎ推論継続の喪失 (モデルが前ターンの思考を忘れる)

**制約 (公式)**: **現在のターン**の function-call parts の thought_signature は剥がしてはいけない (剥がすと 400)。
剥がすのは**過去ターン**のものだけ。

設定粒度: per-persona / per-model / グローバルのいずれか (cache_lifecycle の per-persona 設定と揃えると一貫)。

## 関連リソース

- 公式: [Thought Signatures](https://ai.google.dev/gemini-api/docs/thought-signatures) / [What's new in Gemini 3.5 Flash](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5)
- `llm_clients/gemini.py`: `_store_thought_signature`、thought_signature の保存/再送経路 (剥がすトグルを入れる場所)
- 実機ログ: `~/.saiverse/user_data/logs/20260525_190813/` (cache 55,691 / 入力 66,270 / 非キャッシュ 10,579、18 ターンに thought_signature)
- 検証スクリプト: `temp/verify_gemini_cache_*.py` (B 機構の正常性・media キャッシュ・スケールを実測)

## ログ

- 2026-05-25: 起票。Phase 3 (Gemini explicit cache) の実機検証で発覚。原因確定 (thought signature 非キャッシュ + preservation デフォルト ON)。current code はバグ無しと合意。トグル実装は別途。
