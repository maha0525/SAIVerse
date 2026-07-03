# グローバル設定

SAIVerse 全体の設定。左サイドバー フッターの歯車アイコン（設定）から開く（`GlobalSettingsModal.tsx`）。左のタブで切り替える。

## タブ

| タブ | 内容 |
|---|---|
| **環境** | API キー・環境変数の設定（→ [reference/environment-vars.md](../reference/environment-vars.md)） |
| **ワールドエディタ** | City / Building / ペルソナ / アイテムの編集（→ [ワールドエディタ](world-editor.md)） |
| **データベース管理** | データベースのバックアップ・管理 |
| **モデルロール** | 役割ごとに使うモデルの割り当て（モデルロール設定） |
| **モデル管理** | プロバイダとモデルの追加・編集・接続テスト（→ [reference/providers.md](../reference/providers.md)） |
| **Playbook権限** | Playbook の実行権限設定 |
| **情報** | SAIVerse について（バージョン等） |
| **便利機能** | アイテム概要の一括生成など |

## モデル管理タブ

- **プロバイダ**: 新規追加 → プロトコル選択（`openai_compat` / `ollama_compat`）→ base_url / api_key_env 入力 → 接続テスト
- **モデル**: 新規追加 → JSON で全フィールド編集 → 保存

ビルトインのモデル/プロバイダを編集して保存すると、`~/.saiverse/user_data/` に上書き用のファイルが作られる（ビルトイン本体は変更されない）。

## 関連

- [ワールドエディタ](world-editor.md) - 世界の編集タブ
- [reference/providers.md](../reference/providers.md) / [environment-vars.md](../reference/environment-vars.md)
