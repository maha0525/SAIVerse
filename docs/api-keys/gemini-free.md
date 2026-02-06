# Google Gemini（無料版）APIキーの取得方法

## 1. Googleアカウントでログイン

1. [Google AI Studio](https://aistudio.google.com/) にアクセス
2. Googleアカウントでログイン

## 2. APIキーの生成

1. 左サイドバーの「Get API key」をクリック
2. 「Create API key」をクリック
3. プロジェクトを選択（なければ新規作成）
4. 生成されたAPIキーをコピー

## 3. 無料枠について

Google AI Studioの無料枠では以下が利用可能です：

| 項目 | 制限 |
|------|------|
| リクエスト/分 | 15 RPM |
| トークン/分 | 100万 TPM |
| リクエスト/日 | 1,500 RPD |

### 注意事項
- 無料枠はテスト・開発目的
- レート制限あり
- 本番環境では有料版を推奨

## 4. 利用可能なモデル（無料枠）

- **Gemini 2.5 Flash**: 高速・軽量モデル（推奨）
- **Gemini 2.0 Flash**: バランス型モデル
- **Gemini 1.5 Flash**: 旧世代高速モデル

## 5. 有料版との違い

有料版（Vertex AI経由）では：
- より高いレート制限
- SLA保証
- エンタープライズサポート

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
GEMINI_FREE_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

## 参考リンク

- [Google AI Studio](https://aistudio.google.com/)
- [Gemini API ドキュメント](https://ai.google.dev/docs)
- [料金プラン](https://ai.google.dev/pricing)
