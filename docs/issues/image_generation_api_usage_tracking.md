# Issue: 画像生成系 API 利用が「API 使用状況」にカウントされていない

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `builtin_data/tools/defs/image_generator.py` (相当), API 使用状況集計

## 背景

通常の LLM 呼び出しは API 使用状況 (Usage 表示) に集計されているが、画像生成系の API 呼び出し (Gemini 2.5 Flash Image など) はカウントされていない。

ユーザー視点では「画像生成も API を使っている」のに使用量が見えないため、コスト把握ができない。

## 確認事項

1. 現在の Usage 集計が何を拾っているか — `llm_io.log` ベースか、各 LLM client の hook ベースか
2. 画像生成 Tool が同じ集計経路を通っているか

## 解決案候補

- 画像生成 Tool の API 呼び出しでも Usage record を残す (同じテーブル/ロガー)
- 入力トークン+出力画像枚数で記録 (Gemini はトークン課金、OpenAI gpt-image-1 は枚数+解像度課金など、プロバイダで単位が違う点に注意)

## 関連リソース

- `builtin_data/tools/defs/image_generator.py`
- API 使用状況の集計コード (要特定)

## ログ

- 2026-05-09: issue 起票。
