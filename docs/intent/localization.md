# UI 文言とペルソナ・City の言語設定 (ローカライズ)

> **ステータス**: 起草・設計中 (2026-09-14)
>
> 関連: [アイデア帳 UI文言の日英ローカライズ](../overview/ideas.md) / [landscape §2](../overview/landscape.md) / [city_identity.md](city_identity.md)

## 1. 何を解決するのか

### 課題
1. **文言の修正摩擦**: UI の文言（ボタン、見出し、説明、エラー通知など）がコード内に直書きされており、開発用語が混ざった表現や不自然な言い回しを直すためにコードを直接編集しなければならない。文言表（JSON / CSV）として外出しし、まはー自身が表を見て気になった瞬間に直せるようにしたい。
2. **多言語UI対応**: 日本語と英語を切り替え可能にし、将来的な多言語展開の土台を作る。
3. **ペルソナが暮らす言語の決定**: ペルソナの会話応答や内語（あらすじ・Memopedia・独り言・日記）を何語で生成するかについて、明確な設定経路がなかった。
4. **チュートリアルとCityの言語設定の一致**: 新規導入時（チュートリアル）にユーザーが使用する言語を選択させ、UIの表示言語と最初に作られるCityの言語設定、およびそこで生まれる初期ペルソナの言語設定を自然に一致させる。

### 誰が影響を受けるか

| 立場 | 何を頼りにできるようになるか |
|---|---|
| **まはー (ユーザー)** | 画面の不自然な日本語や開発用語を、コードを触らず文言表（またはCSV）で直せる。開発モードでマウスホバーすればどの行か即座にわかる。 |
| **利用者** | UI 表示言語（日本語 / English）を自由に切り替えられる。チュートリアルで選んだ言語で即座に街とUIが立ち上がる。 |
| **ペルソナ** | 自分が暮らす言語（会話応答・思考・日記・あらすじ・Memopedia）を正しく自覚して振る舞える。生まれたCityの言語を既定としつつ、必要に応じて個別設定できる。過去の記憶は翻訳・改変されず無垢に保たれる。 |
| **開発者・保守者** | フロントエンドとバックエンドの文言が分離され、API は識別子とパラメータのみを返し、表示責任はUI側が持つ設計になる。 |

---

## 2. 芯の要約 (把握可能性)

> **「UI の言葉は文言表で直せ、ペルソナの言葉は生まれた街から受け継ぐ。」**
> 
> 画面の固定文言はすべて外出しの文言表で管理し、表示言語をいつでも切り替えられる。
> 街 (City) は言語を持ち、チュートリアルでタイムゾーンと一緒に設定される。
> ペルソナは生まれた街の言語を既定として受け継ぎ、その言語で話し、思考し、記憶（あらすじやMemopedia）を紡ぐ。

---

## 3. 全体構造とデータフロー

```mermaid
flowchart TD
    subgraph Tutorial [チュートリアル (初回設定)]
        T1["言語選択 (日本語 / English)"]
        T2["タイムゾーン設定"]
        T1 -->|即時反映| UI_Lang["UI 表示言語 (localStorage)"]
        T1 -->|保存| City_Lang["City.LANGUAGE"]
    end

    subgraph CityScope [街 (City)]
        City_Lang -->|既定値として継承| Persona_Default["新規ペルソナの既定言語"]
    end

    subgraph PersonaScope [ペルソナ (AI)]
        Persona_Default --> Persona_Lang["AI.LANGUAGE (個別変更可能)"]
        Persona_Lang --> HeadPrompt["Head Pipeline: Language of your life 指示"]
        Persona_Lang --> MemoryGen["記憶生成: あらすじ / Memopedia / 日記"]
    end

    subgraph UI_System [フロントエンド UI]
        UI_Lang --> i18n["i18n 表 (messages.json / CSV)"]
        i18n --> Components["画面コンポーネント (t('key'))"]
        API_Msg["API 操作結果 ({$ui: 'key', params})"] --> Components
        DevMode["開発モード (ホバーでキー表示)"] -.-> Components
    end
```

---

## 4. 所有と不変条件

1. **記憶の無垢を守る (最重要)**:
   - 言語設定（UI言語、City言語、ペルソナ言語）を変更しても、**過去に記録された発言ログ、あらすじ、Memopedia、日記、タスク帳などの既存テキストを機械翻訳・書き換えしてはならない**。
   - 新しい言語指示は、変更「以降」に生成される発言・内語・記憶にのみ適用される。
2. **システムプロンプト本体の純粋性**:
   - 共通システムプロンプト（`common.txt` 等）や Playbook のノード定義は**日本語のまま全言語共通**とする。プロンプト自体を多言語化するとLLMの挙動ドリフトや保守コストが爆発するため。
   - ペルソナの出力言語は、Head Pipeline（`persona_self.py`）および記憶生成プロンプトの直前に「Language of your life: ...」という明確な環境情報として指示を付与する。
3. **文言表が UI 固定文言の唯一の正典**:
   - UI コンポーネント内の固定文字列は文言表（`messages.json`）を参照する。
   - 文言キーを DB 保存データの識別子（Enum、ステータス名、Slug 等）として流用しない。
4. **API の表示責任分離**:
   - API は画面に表示する完成文（日本語ベタ書き）を返さない。
   - UI 表示用のメッセージは `{"$ui": "key", "params": {...}}` という形式で返し、文の組み立てはフロントエンド側の文言表が行う。
5. **City とペルソナの言語の独立性**:
   - ペルソナ作成時の初期値は「生まれた City の言語設定」とする。
   - ペルソナ作成後は、ペルソナ設定画面からペルソナ単体で言語を変更できる（バイリンガルな街、あるいは異なる母語を持つペルソナの同居を可能にする）。
   - City の言語を変更しても、既存ペルソナの言語は勝手に書き換えない。

---

## 5. 詳細設計

### 5-1. データベース層 (`database/models.py`, `database/migrate.py`)

- **`City.LANGUAGE`**:
  - `Column(String(16), default="ja", nullable=False)`
  - 既存 DB のマイグレーション: `ALTER TABLE city ADD COLUMN LANGUAGE VARCHAR(16) DEFAULT 'ja'`（存在しない場合のみ安全に追加）。
- **`AI.LANGUAGE`**:
  - `Column(String(16), default=None, nullable=True)`
  - ペルソナ作成時に明示的に設定される（初期値: 所属 City の `LANGUAGE`）。
  - NULL の場合は所属 City の `LANGUAGE`、それも無ければ `'ja'` にフォールバックする安全な取得関数 `get_persona_language(persona_id)` を提供。
  - 既存 DB のマイグレーション: `ALTER TABLE ai ADD COLUMN LANGUAGE VARCHAR(16) DEFAULT NULL`。

### 5-2. チュートリアル (`StepCityName.tsx`, `TutorialWizard.tsx`, `api/routes/tutorial.py`)

- **ステップ配置**:
  - `StepCityName.tsx`（都市名入力）内に「タイムゾーン」と並んで「言語 (Language)」の選択フォームを配置。
  - 初期値判定: ブラウザの `navigator.language` を参照し、`ja` 系なら「日本語」、それ以外なら「English」を初期選択。
- **即時反映 & 保存**:
  - ユーザーが言語を変更した瞬間、`setLocale(lang)` を呼び出してフロントエンドの表示言語を切り替える（チュートリアル中の文言も即座に選択した言語に変化する）。
  - チュートリアル完了時、City の更新 API（`POST /api/tutorial/city` または `PUT /api/world/city`）に `timezone` と `language` を送信し、City に保存。
  - 初期ペルソナ作成時（`StepPersonaChoice.tsx` 等）にも、この言語がペルソナの `LANGUAGE` として渡される。

### 5-3. ペルソナ設定 UI & API (`PeopleModal.tsx`, `api/routes/people/config.py`)

- ペルソナ編集画面（`PeopleModal`）の基本設定タブに「使用する言語」項目を追加（選択肢: 日本語 / English）。
- `GET /api/people/{id}/config` で `language` を返却。
- `PATCH /api/people/{id}/config` で `{"language": "en"}` を更新可能にする。

### 5-4. ペルソナ生成パイプラインへの反映

1. **会話・自律 Pulse (Head Pipeline)**:
   - `sea/head_pipeline/sections/persona_self.py`:
     - ペルソナの言語に応じた指示を生成し、Head に注入する。
     ```markdown
     ## Language of your life: 日本語 (Japanese)
     Write your replies, private thoughts, diary, Chronicle summaries and Memopedia prose in 日本語 (Japanese), regardless of the language of these shared instructions. Preserve original quotations, proper names, identifiers, tool arguments and JSON keys. Do not translate or rewrite existing memories. Follow an explicit request to use another language for a particular response.
     ```
2. **記憶生成パイプライン (Chronicle / Memopedia / ノート)**:
   - `sai_memory/arasuji/generator.py`
   - `sai_memory/memopedia/generator.py`
   - `sai_memory/memory/note_executor.py`
   - 生成リクエストの system messages に `language_instruction(language)` を追加し、ペルソナの言語で記憶を執筆・編纂させる。

### 5-5. フロントエンド i18n 基盤

1. **文言表 (`frontend/src/i18n/`)**:
   - `messages.json`: 各キーに対応する `ja` / `en` のテキストを保持。
   - `locales.json`: 利用可能言語の一覧と書式設定（`ja`, `en`）。
   - `core.ts`: `t(key, params)`, `getLocale()`, `setLocale(lang)`, `subscribeLocale(fn)`。
   - `useLocale.ts`: React hook。
2. **開発モードのキー表示ツールチップ (`LocaleBridge.tsx`)**:
   - 開発モード（`developerMode === true`）の際、`data-i18n` 属性を持つ要素にマウスホバーすると、該当する翻訳キーをツールチップで画面端に表示。
   - まはーが「この日本語の表現を直したい」と思ったときに、キーを即座に特定できる。
3. **CSV ツール (`frontend/scripts/i18n-table.py`)**:
   - `messages.json` ⇄ CSV の双方向変換スクリプト。
   - まはーがスプレッドシートやエディタで一括推敲・改善を行える。

---

## 6. 段階的実装計画 (フェーズ)

- **Phase 1: DBスキーマ・City/ペルソナ言語基盤 & Intent確定**
  - DBモデル拡張（`City.LANGUAGE`, `AI.LANGUAGE`）、マイグレーション処理。
  - `saiverse/persona_language.py`（言語解決ヘルパー、プロンプト生成）。
  - Head Pipeline および記憶生成への言語反映。
  - バックエンド単体テスト。
- **Phase 2: チュートリアルと設定 UI の配線**
  - `StepCityName.tsx` への言語選択追加、即時UI切り替え。
  - City 保存処理への言語反映、初期ペルソナへの言語引き継ぎ。
  - ペルソナ設定モーダル（`PeopleModal`）での言語編集UI。
- **Phase 3: フロントエンド i18n 基盤 & 開発ツールチップ**
  - `frontend/src/i18n/` 基盤の整備。
  - API メッセージの `ui_message` 構造化。
  - 開発モードのホバーツールチップと CSV ツール。
- **Phase 4: 文言の推敲・洗練 (Gemini での表現改善)**
  - まはーと一緒に文言表を点検し、機械翻訳やクセのある日本語を、SAIVerse の世界観に合った自然で温かみのある日本語表現にブラッシュアップ。

---

## 7. 検証計画

1. **DB マイグレーション検証**: 既存 DB に `LANGUAGE` カラムが安全に追加され、既存データが壊れないこと。
2. **チュートリアル導線検証**:
   - チュートリアルで言語を「English」に変更した際、UI が即座に英語になり、作成された City とペルソナが `en` になること。
   - 日本語を選択した際、日本語のまま進行すること。
3. **ペルソナ言語の独立検証**:
   - City が `ja` でペルソナを `en` に個別変更した場合、そのペルソナのプロンプトに英語指示が渡ること。
4. **記憶生成の言語検証**:
   - 隔離テスト環境において、ペルソナの言語設定に応じたあらすじ・Memopedia 生成リクエストが正しく構築されること（本番ペルソナには接触しない）。
5. **開発モードのツールチップ検証**:
   - 開発モード有効時に文言ホバーで翻訳キーが確認できること。
