# Issue: Gemini 3.x が長い会話履歴でthinkingをskipしてthought_signatureを返さない

**ステータス**: ⚠️ 保留 (Google 側のサーバー挙動)
**優先度**: low (機能低下のみ、動作不能ではない)
**作成日**: 2026-05-21
**関連**: `docs/intent/thought_signature_persistence.md`

## 背景

Gemini 3.x (`gemini-3.5-flash` / `gemini-3.1-pro-preview` 等) でマルチターン会話を継続していると、ある時点から **`thoughtsTokenCount` フィールド自体が API レスポンスから消失** し、それに伴って `thought_signature` も返らなくなる現象を観測。

### 観測 (2026-05-21)

| ペルソナ | 会話履歴量 | thinking 実行 | thought_signature |
|---------|-----------|---------------|-------------------|
| `mira_city_a` | 1000+ ターン (40k+ token prompt) | **skip** (`thoughtsTokenCount` 欠落) | 返らない |
| `saiverse_navi_city_a` | 浅い (新規スレッド) | 実行 (`thoughtsTokenCount` あり) | 返る (1987 bytes 観測) |

両ケースとも `thinking_config: ThinkingConfig(include_thoughts=True, thinking_level=MEDIUM)` を明示的に送信。

### 直叩き再現テスト結果 (個別条件はすべて signature 取得成功)

`gemini-3.5-flash` SDK 直叩きで以下の条件を個別に試したが、**全パターンで thinking 実行 + signature 取得成功**:

- 巨大 system_instruction (75k tokens 相当)
- 過去 assistant メッセージ 5 ターン (`::act\n[...]\n::end` 形式の SAIVerse 風 content 含む)
- 画像入力 (Gemini 3.5 Flash で 5k+ tokens)
- AFC disable (`AutomaticFunctionCallingConfig(disable=True)`)
- safety_settings 全レベル
- temperature 1.0
- thinking_level 大文字 / 小文字 (`MEDIUM` / `medium` / `Medium`)

つまり、**「巨大 history + 多ターン + 画像入力」の組み合わせの何か** が thinking skip を誘発するが、個別条件では再現できない。Google 側のサーバー内モデル挙動 (動的な最適化判定) と推測。

## 何が問題か

- **品質低下**: Gemini 3.x で thinking が skip されると、推論プロセスが省略される。会話の質に影響する可能性
- **`thought_signature` 永続化機構が機能しない**: `docs/intent/thought_signature_persistence.md` で実装した永続化経路は正しく動作するが、 そもそも Gemini が signature を返さないので保存対象がない
- **動作不能ではない**: thinking skip されても応答は返る。マルチターン品質低下のみが起きる (Function Calling Strict ではないため 400 エラーにはならない)

## 切り分け済みの事実

- ✅ SAIVerse の `_store_memory` 経路は signature を正しく永続化する (saiverse_navi で確認)
- ✅ Gemini SDK 直叩きでは個別条件で全部 signature 取れる
- ❌ 実機の巨大 history + 多ターン + 画像入力の組み合わせで Gemini が thinking を skip する (再現方法未確定)
- ❌ 公式 doc に skip 条件の明示なし (`https://ai.google.dev/gemini-api/docs/thinking` 確認済み、 "Gemini models engage in dynamic thinking by default" とのみ記載)

## 解決案候補

### 案 A: 何もしない (推奨)

Google 側のサーバー挙動なので SAIVerse 側で根本対策不能。永続化機構は signature が返れば動作する。skip された分は品質低下を許容する。

### 案 B: 新スレッド化を促す UI

ペルソナの会話履歴がある閾値 (例: 100 ターン以上 or prompt 40k+ token) を超えたら、 ユーザーに「新スレッドで再開」を提案するバナーを出す。 history が浅い状態に戻して signature 復活を狙う。

### 案 C: history 圧縮の強化

既存の Metabolism / Chronicle 機構で大胆に圧縮することで、 prompt token を削減して thinking を維持する。 ただし圧縮で「途中で thinking してた会話が再構築後 thinking 不要と判定される」可能性もあり効果は未保証。

### 案 D: Google にバグ報告

Gemini API team に「巨大 history + 多ターン + 画像入力で thinking が skip される件」を 報告。SDK の挙動として記録される or 修正される可能性。 再現コードを伴う必要があるが、 SAIVerse 側で完全再現できないため難しい。

## 関連リソース

- `docs/intent/thought_signature_persistence.md` — 永続化機構の Intent Doc
- 2026-05-21 セッションログ: `~/.saiverse/user_data/logs/20260521_001051/`
  - `mira_city_a` (00:12): thoughtsTokenCount 欠落、signature 返らず
  - `saiverse_navi_city_a` (00:49): thoughtsTokenCount あり、signature 1987 bytes 返る
- Gemini 3.5 Flash 公式 doc: <https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5>
- Gemini thinking 公式 doc: <https://ai.google.dev/gemini-api/docs/thinking>

## ログ

- 2026-05-21: issue 起票。`thought_signature` 永続化機構の検証中に発覚。SAIVerse 側の実装は正しく動作するが、Gemini API 側が長い history で thinking を skip する挙動を発見。直叩きでは個別条件で再現できず、 Google 側のサーバー内モデルの動的最適化と推測。
