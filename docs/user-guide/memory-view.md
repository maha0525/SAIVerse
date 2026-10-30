# メモリービュー（記憶モーダル）

ペルソナの記憶を閲覧・管理する画面。ペルソナメニュー →「記憶」で開く（`MemoryModal.tsx`、タイトルは「〈ペルソナ名〉のメモリー」）。上部のタブで切り替える。

## タブ

| タブ | 内容 |
|---|---|
| **チャットログ** | 生の会話ログをスレッド単位で閲覧（アクティブスレッド / その他のスレッド）。メッセージの追加も可能 |
| **Chronicle** | あらすじ（Chronicle）をレベル別に閲覧（→ [concepts/chronicle.md](../concepts/chronicle.md)） |
| **Memopedia** | 知識ページ（→ [Memopedia](memopedia.md)） |
| **7層ストレージ** | 記憶の内部階層を Track 単位で確認する可視化（開発者寄りの調査ビュー） |
| **Tracks** | ペルソナの行動 Track 一覧（→ [concepts/track.md](../concepts/track.md)） |
| **Pulse タイムライン** | Pulse の時系列（→ [concepts/pulse.md](../concepts/pulse.md)） |
| **引っ越し** | 上から順に、外部チャットログ（ChatGPT / Claude 公式エクスポート等）の取り込み、ペルソナまるごとパックの取り込み（ZIP をこのペルソナへ合流させる）、ペルソナまるごとパックのエクスポート（このペルソナを ZIP にまとめる）、エンベディング管理（未作成メッセージへの埋め込み生成）。チャットログの手順は [他サービスからの引っ越し](memory-migration.md)、パックの仕様は [persona_pack.md](../intent/persona_pack.md) |
| **デバッグ** | セマンティック想起のテスト（検索クエリを投げて確認）、Stelis スレッド救出などの調査用 |

## よく使うタブ

- **チャットログ / Chronicle / Memopedia** — ペルソナが「何を覚えているか」を見る主な導線
- **引っ越し** — 他サービスからの引っ越し（過去の対話履歴を取り込む）と、ペルソナまるごとパックの取り込み・エクスポート
- 残り（7層ストレージ / Tracks / Pulse タイムライン / デバッグ）は主に内部状態の確認・調査用

## 関連

- [Memopedia](memopedia.md) - Memopedia タブの詳しい使い方
- [他サービスからの引っ越し](memory-migration.md) - 引っ越しタブ / 引っ越しスクリプトの詳しい手順
- [concepts/saimemory.md](../concepts/saimemory.md) - 記憶システムの全体像
