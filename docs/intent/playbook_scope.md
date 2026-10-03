# Intent: Playbook の保存先推論を CLI から切り離す

> **ステータス**: 検証待ち (隔離テスト済み、レビュー待ち)
> **根拠**: [アーキテクチャ健診 §3.3](../overview/architecture_health.md#33-p2-全パッケージ循環--横断ユーティリティが-saiverse-パッケージに居る)

## 全体と境界

利用者がファイルを管理画面または CLI から取り込むとき、同じ配置なら同じ
公開範囲・所有ペルソナ・対象建物として保存されることを守る。
経路は「JSON ファイルのパス → scope と所有者の推論 → 呼び出し元の明示指定
(CLI のみ) → `save_playbook` による検査・保存」。本変更は推論の置き場所だけを変える。

推論は `saiverse/playbook_scope.py` が所有し、管理サービスと CLI が同じ関数を使う。
標準ライブラリ以外に依存せず、CLI の読み込み・`sys.path` の変更・保存処理の
読み込みや呼び出しを行わない。`sea/__init__.py` はランタイムを読み込むため、
この小さな共有関数の置き場所にはしない。新しい共通パッケージの導入や、
ほかの import 循環の整理は本変更に含めない。

## 保つ振る舞い

- `Path.resolve()` 後の最初の `playbooks` 要素を基準にする。
- 直後が `public` なら `("public", None, None)`。
- 直後が `personal` / `building` で次の要素があれば、その要素を所有者 ID にする。
- `playbooks` がない・範囲が未知・所有者要素がない場合は public に戻す。
- builtin / user / addon / 旧 `sea/playbooks` の配置は同じ規則。
- ファイルの存在・拡張子・所有者の妥当性を推論層で検査しない。既存の境界を保ち、
  取り込み側のファイル検査と保存側の検査を変えない。
- CLI の明示引数の優先順位、管理画面の単一/一括取り込み、保存処理は変更しない。

この切り離しで `manager → scripts.import_playbook` はなくなるが、
管理サービスが保存のために `save_playbook` を読む依存や、健診にある他の循環は残る。

## 検証

- 配置別・不完全な配置・相対パス・symlink の既存結果を単体テストで固定する。
- 新規 Python プロセスで共有関数の import が CLI / 保存処理を読み込まず、
  `sys.path` を変えないこと、管理サービスの import が CLI に依存しないことを検べる。
- CLI の引数処理と管理サービスの単一/一括取り込みを実コードで通し、
  `save_playbook` に届く引数を fake で検べる。実 DB への取り込み、ペルソナ起動、
  LLM 通信は行わない。実画面と本番 DB の経路は未検証として引き渡す。

## 経緯

- 2026-10-03: 健診に記載された小さな逆流 import の解消として、挙動を変えない
  共有関数抽出に着手。新しい公開範囲の仕様は導入しない。
- 2026-10-03: 共有関数への移動と両入口の切り替えを実装。パス互換・fresh-process
  import・保存を fake にした CLI/管理サービスの 36 テストが通過。実 DB への
  取り込みや実画面の操作は行っていない。次は差分と隔離検証結果のレビュー。
- 2026-10-03: 既存の同期・更新検査も含めて
  `pytest tests/test_playbook_scope.py tests/test_playbook_sync.py tests/test_playbook_update_validation.py -n 0`
  は 47 件通過 (一時 `SAIVERSE_HOME`、`SAIVERSE_SKIP_TOOL_IMPORTS=1`)。
  変更 Python の `ruff check` と台帳検査も通過。全体テストは未実行。
