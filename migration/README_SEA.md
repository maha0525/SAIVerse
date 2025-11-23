SEA Playbook 永続化のためのテーブル追加案 (まだ適用していません)

- 新テーブル `playbooks`
  - id (INTEGER PK AUTOINCREMENT)
  - name TEXT UNIQUE NOT NULL
  - description TEXT
  - scope TEXT CHECK(scope IN ('public','personal','building')) NOT NULL DEFAULT 'public'
  - created_by_persona_id TEXT NULL
  - building_id TEXT NULL
  - schema_json TEXT NOT NULL
  - nodes_json TEXT NOT NULL
  - created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  - updated_at DATETIME DEFAULT CURRENT_TIMESTAMP

- フィルタリングロジック
  - public: 全 persona が利用可
  - personal: created_by_persona_id == persona_id のときのみ利用可
  - building: building_id == 現在地のときのみ利用可（将来）

- TODO
  - `python database/migrate.py --db database/data/saiverse.db` で既存DBに適用
  - save_playbook ツールをファイル保存→DB insert に差し替え
  - Router が DB から permitted playbooks をロードする実装
