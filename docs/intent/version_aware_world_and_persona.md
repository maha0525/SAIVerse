# Intent Document: 世界とペルソナのバージョン認識

## 概要

SAIVerse のシティとペルソナがそれぞれ「自分が最後に通ったバージョン」を記憶し、アップデート時に必要な状態移行処理（マイグレーション）を確実かつ冪等に実行できるようにするための設計。

主な動機は、コードのアップデートがペルソナの長期状態に対して非互換な変更を起こすケース（dynamic_state、認知モデル、playbook フォーマット等）に、再現可能で安全に対処すること。

---

## なぜ必要か（解決する問題）

### きっかけ

`dynamic_state_sync.md` で定義された A/B/C 3状態モデルは、Memopedia の差分判定をタイムスタンプベース化する改修により、`PersonaBuildingState.LAST_NOTIFIED_JSON` の `captured_at` を「since」として使う設計に変わった。

この改修をリリースすると、既存ユーザーの環境では:
- 旧 `captured_at` が古いまま残っている
- 新実装では「`b.captured_at` 以降の Memopedia 全変化」が一気に通知される
- ペルソナにとって「アップデート直後に大量の変化通知が降ってくる」体験になる

これを避けるため、リリース時に各ペルソナの `captured_at` を現在時刻にリセットしたい。しかし、`database/migrate.py` はスキーマ比較で動く方式で、マイグレーション履歴を持たない。そのため、別件のスキーマ変更が走るたびに同じリセット処理も走ってしまう。

### 本質的な問題

**「アップデートを跨いだ状態移行」を確実に1回だけ実行する仕組みが SAIVerse にはない**。

dynamic_state の例に限らず、今後も同種の問題は繰り返し起きる:
- 認知モデル（Track / Note / ライン）のスキーマ変化
- playbook フォーマットの後方非互換変更
- SAIMemory のスキーマ進化
- 自律稼働バイオリズム（Phase 3）導入時の状態追加

毎回個別にスクリプトを書くのではなく、**「ペルソナ・シティが自分の通ったバージョンを記憶し、必要な移行処理だけを実行する」**汎用基盤として整備する。

---

## 設計

### バージョン保持の単位：City + AI 両方

| 単位 | 保存場所（提案） | 用途 |
|---|---|---|
| **City** | `City.LAST_KNOWN_VERSION` | シティ全体に対する処理（DB スキーマに伴う一括処理、共有設定の補正など）。1シティで1回だけ走らせたいタスク |
| **AI（ペルソナ）** | `AI.LAST_KNOWN_VERSION` | ペルソナ個別の状態に対する処理（SAIMemory の構造変更、Memopedia の同期、認知モデルの整合化など）。ペルソナごとに走らせたいタスク |

City単位だけだと、シティに紐づく全ペルソナの個別処理を一気に行わなければならず厄介。AI単位だけだと、ペルソナ非依存の問題に対する対応場所がない。両方持たせることで、ハンドラを書く側が「City単位 / AI単位」を選べる。

### バージョン文字列

[Semantic Versioning](https://semver.org/) を採用。比較には `packaging.version.Version` を用いる。

`saiverse/__init__.py` 等に `__version__ = "0.3.0"` を定義し、これが「現在のSAIVerseバージョン」となる。

### 起動時のバージョン比較フロー

`main.py` の起動シーケンスに以下を組み込む:

1. `__version__` を読み込み（current_version）
2. `City.LAST_KNOWN_VERSION` を取得（city_version）
3. `city_version < current_version` ならシティ単位のアップデートハンドラを順次実行
4. シティ内の各ペルソナについて:
   - `AI.LAST_KNOWN_VERSION` を取得（ai_version）
   - `ai_version < current_version` ならペルソナ単位のアップデートハンドラを順次実行
5. 全て成功したら `LAST_KNOWN_VERSION` を `current_version` に更新

### アップデートハンドラの責務

各ハンドラは「あるバージョン範囲で走るべき処理」を定義する。

```python
@dataclass
class UpgradeHandler:
    name: str                  # 識別子（"v0_3_0_dynamic_state_reset" 等）
    scope: Literal["city", "ai"]
    from_version: str          # この版より前から来た場合に走る
    to_version: str            # この版で導入された処理
    run: Callable              # 実体
```

初期実装ではハンドラは Python のリテラルリストで一覧化（登録式は将来拡張）。

### スキップ済みバージョンの扱い：中間ハンドラを順次実行

例: v0.2.5 → v0.4.0 へのアップデート時、v0.3.0 用ハンドラと v0.4.0 用ハンドラの両方を順番に実行する。

理由:
- これがないと、各ハンドラが「どのバージョンから来てもOK」を保証する必要があり、雪だるま式にハンドラ内容が肥大化する
- 順次実行であれば、各ハンドラは「直前のバージョンから来た想定」だけ書けばいい
- 中間で失敗した場合も「どこで止まったか」が `LAST_KNOWN_VERSION` から追える（部分復旧可能）

### ハンドラの不変条件

- **冪等性必須**: 同じハンドラを複数回実行しても結果が同じになる。バグ修正後の再実行に対応するため
- **副作用は局所化**: 触るべき範囲（City単位なら自シティ、AI単位なら自ペルソナ）から逸脱しない
- **失敗時は例外を上げて中断**: バージョン更新は最後の最後に行い、ハンドラのどれかが失敗したら `LAST_KNOWN_VERSION` を進めない

---

## シティ間移動時のバージョン制約（暫定）

### 暫定ルール

**シティとバージョンが違うペルソナは、他シティからの来訪を拒否する。**

理由: バージョン食い違いを真面目に解こうとすると、以下の論点が一度に発生して範囲が広がる:
- 来訪元シティの古いバージョン基準で動いているペルソナの状態を、来訪先シティの新バージョンに合わせて補正する処理が必要
- 補正タイミング（来訪前 / 来訪時 / 帰還時）の判断
- 補正失敗時のフォールバック
- SDS でのバージョン情報のやり取り

これらは将来課題として切り出し、当面は「シティ間でバージョンが揃っていることを前提とする」ことで穴を塞ぐ。

シティ内では、**まだアップデート処理が走っていないペルソナがいる状態は許容する**（アップデートは順次実行されるため、過渡期は必然的に発生する）。

### 将来の拡張ポイント

- マルチシティ環境での協調アップデート
- バージョン違いペルソナの来訪時自動補正
- SDS 経由でのシティ間バージョン情報共有

---

## テスト・デバッグ基盤

バージョンは不可逆なため、本体実装の前にテスト・デバッグ基盤を整備する必要がある。

### スナップショット / 復元機能

SAIVerse 停止状態で動く独立スクリプト + bat/sh ラッパー。

#### スナップショット対象

`~/.saiverse/` 全体を対象とし、以下のみ除外:
- `~/.saiverse/backups/` — 過去バックアップ
- `~/.saiverse/user_data/logs/` — セッションログ

#### スナップショットメタ情報

`snapshot.json` をアーカイブ内に同梱:

```json
{
  "name": "before_v0_3_0_upgrade",
  "created_at": "2026-04-27T01:23:45+09:00",
  "saiverse_version": "0.2.5",
  "city_versions": {"city_a": "0.2.5", "city_b": "0.2.5"},
  "persona_versions": {"aifi_city_a": "0.2.5"},
  "note": "ユーザー記述の自由メモ"
}
```

#### CLI

```bash
./snapshot.sh save <name> [--note "..."]
./snapshot.sh list
./snapshot.sh restore <name>     # 復元前に「現状」を自動スナップショット
./snapshot.sh delete <name>
./snapshot.sh inspect <name>     # snapshot.json を表示
```

実体は `scripts/snapshot.py`、bat/sh は薄いラッパー。

#### 安全装置

- **起動中の SAIVerse を検出**: pid ファイル or DB ロック取得を試みる。動作中なら警告して中止
- **復元前自動スナップショット**: 復元操作で現状が消える前に自動で `auto_before_restore_<timestamp>` を取る
- **ハッシュ検証**: アーカイブのハッシュをメタに記録、復元時に整合性確認

### バージョン書き換えコマンド

ハンドラのテストで「v0.2.5 から来たことにしたい」場面が頻発する。直接DBを操作する補助スクリプト。

```bash
python scripts/set_version.py --persona <id> --to 0.2.5
python scripts/set_version.py --city <id> --to 0.2.5
python scripts/set_version.py --all --to 0.2.5    # 全City+全AI一括
```

### ハンドラ単独実行 / ドライラン

```bash
python scripts/run_upgrade_handler.py <handler_name> --persona <id>
python scripts/run_upgrade_handler.py <handler_name> --city <id> --dry-run
```

`--dry-run` ではハンドラの効果をログに出力するだけで実際の変更は行わない。冪等性の確認にも使える（2回実行して差分が出ないことを確認）。

### 起動時バージョンチェックのスキップ

`SAIVERSE_SKIP_VERSION_CHECK=1` 環境変数で起動時のバージョン比較フックを完全スキップ。デバッグ・開発時に「アップデート処理を走らせずに古い状態のまま動かす」ためのもの。

---

## 段階的実装計画

### フェーズ0: スナップショット / 復元基盤

最初に作る。これがないと以降のフェーズのテストが安全に行えない。

- [ ] `scripts/snapshot.py` 実装
- [ ] `snapshot.bat` / `snapshot.sh` 作成
- [ ] 動作中検出・復元前自動スナップショット
- [ ] スナップショットメタ情報のフォーマット定義

### フェーズ1: バージョン保持カラム + 起動時比較

- [ ] `database/models.py` に `City.LAST_KNOWN_VERSION`, `AI.LAST_KNOWN_VERSION` 追加
- [ ] `saiverse/__init__.py` に `__version__` 定義
- [ ] `saiverse/upgrade.py` 新設（バージョン比較・ハンドラ実行ロジック）
- [ ] `main.py` の起動シーケンスにフック挿入
- [ ] `scripts/set_version.py` 実装
- [ ] `SAIVERSE_SKIP_VERSION_CHECK` 対応

### フェーズ2: アップデートハンドラの仕組み + 第1号

- [ ] `UpgradeHandler` データクラス定義
- [ ] ハンドラリスト（`saiverse/upgrade_handlers.py`）
- [ ] `scripts/run_upgrade_handler.py` 実装（`--dry-run` 対応）
- [ ] **第1号ハンドラ**: v0.3.0 用 `dynamic_state_captured_at_reset`（AI単位）
  - 各ペルソナの `PersonaBuildingState.LAST_NOTIFIED_JSON` の `captured_at` を現在時刻に書き換え
  - `memopedia_pages` を空配列に置換
  - SAIMemory に「v0.2.x → v0.3.0 へのアップデートを検知。Memopedia の状態同期がリセットされました」通知を1件挿入

### フェーズ3: 必要に応じて拡張

- [ ] ハンドラ登録式（デコレータ等）
- [ ] バージョン違いペルソナの来訪時自動補正
- [ ] SDS 経由でのシティ間バージョン情報共有

---

## 不変条件まとめ

- バージョンは不可逆（一度上がったら戻せない、これが設計の最大リスク）
- ハンドラは冪等であること
- ハンドラは `from_version → to_version` の隣接遷移のみを想定して書く（飛び級は基盤側で順次実行する）
- バージョン更新は全ハンドラ成功後にのみ実行
- 復元なしの実機テストはしない（バックアップなしで実行された場合のリカバリ手段がない）

---

## 関連ドキュメント

- `dynamic_state_sync.md` — 本設計の動機となった A/B/C 3状態モデル
- `unified_memory_architecture.md` — Phase 2 以降のスキーマ変更でこの基盤が必要になる
- `persona_cognitive_model.md` — 認知モデル拡張時のマイグレーション需要
