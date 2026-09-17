# 多言語化（i18n）・UI文言メンテナンス手順書

SAIVerse のフロントエンド UI は、日本語（`ja`）および英語（`en`）の多言語対応に対応しています。
UI 文言は `frontend/src/i18n/messages.json` を正典（Canonical）として管理されており、CSV 経由でスプレッドシートや Excel を用いて一括で編集・推敲することができます。

---

## 1. ディレクトリとファイル構成

- **`frontend/src/i18n/messages.json`**:
  全 UI 文言の正典辞書。キーごとに `{"ja": "...", "en": "..."}` を保持。
- **`frontend/src/i18n/locales.json`**:
  サポート言語一覧（`ja`, `en`）の定義。
- **`frontend/src/i18n/core.ts`**:
  翻訳関数 `t(key, params)` や言語切り替え関数を提供するランタイム。
- **`frontend/scripts/i18n-table.py`**:
  `messages.json` と CSV ファイルを相互変換するメンテナンススクリプト。
- **`frontend/scripts/test-i18n.cjs`**:
  `npm test` 実行時に走る整合性検証スクリプト（未使用キー、未翻訳、全角記号・CJK 直書き漏れの検知）。

---

## 2. CSV を使った文言の編集ワークフロー

Excel や Google スプレッドシートを使って文言を点検・修正する基本手順です。

### ステップ 1: CSV のエクスポート

リポジトリルートで以下のコマンドを実行します。

```bash
python frontend/scripts/i18n-table.py export wording.csv
```

- リポジトリルートに `wording.csv` が出力されます。
- **UTF-8 BOM 付き** で出力されるため、Windows の Excel でダブルクリックして開いても文字化けしません。

### ステップ 2: スプレッドシート / Excel での編集

CSV のカラム構成：
| key | ja | en |
| :--- | :--- | :--- |
| `app.page.text001` | ホームに戻る | Back to Home |
| `item.detail.count` | 残り {count} 個 | {count} left |

#### 編集時の注意点・禁止事項
1. **`key` 列は絶対に書き換えない・削除しない・追加しない**:
   - キーの同一性はインポート時に厳密に検証されます。
2. **変数プレースホルダー `{...}` を保持する**:
   - 例: `{name}` や `{count}` などの波括弧で囲まれた変数は、プログラムから動的に値が埋め込まれます。
   - `ja` と `en` の間で変数の種類・個数が一致している必要があります（不一致があるとインポート時にエラーになります）。
3. **改行の扱い**:
   - セル内改行を含める場合は、通常の CSV 引用符（`"..."`）を崩さないように注意してください。

### ステップ 3: CSV のインポート（反映と自動検証）

編集を保存した CSV をリポジトリに反映します。

```bash
python frontend/scripts/i18n-table.py import wording.csv
```

- スクリプトが自動的に以下を厳密に検証します：
  - キーの欠落・重複がないか
  - 変数プレースホルダー（`{param}`）が日米間で完全に一致しているか
  - 空のテキストがないか
- 検証に合格すると、`frontend/src/i18n/messages.json` が安全に更新されます。

---

## 3. 検証コマンド

文言を反映した後は、以下の検査コマンドを実行して壊れがないことを確認します。

```bash
# 1. フロントエンドの i18n 整合性検査（未使用キー、構文エラー、直書きの検知）
cd frontend
npm test

# 2. TypeScript の型チェック
npx tsc --noEmit
cd ..

# 3. バックエンド側の多言語関連ユニットテスト
pytest tests/test_localization.py tests/test_i18n_utils.py
```

---

## 4. 画面上で文言のキーを特定する方法（開発モードツールチップ）

「ブラウザ画面で見えているこの日本語を直したいが、どのキーか分からない」という場合は、開発者ツールチップを活用します。

1. SAIVerse を起動し、ブラウザで設定画面を開きます。
2. **「開発者モード（Developer Mode）」を有効化** します。
3. 画面上のボタンやラベルにマウスカーソルをホバーすると、画面隅に該当する翻訳キー（例: `settings.persona.title`）がツールチップ表示されます。
4. 特定したキーを `wording.csv` や `messages.json` で検索して修正できます。
