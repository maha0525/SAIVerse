# Handoff: `report_to_parent` 必須バリデーション (`can_run_as_child=true` 用)

**親**: [README.md](README.md)
**ステータス**: ✅ 完了 (2026-05-11)
**経緯**: [02_mechanics.md](02_mechanics.md) §「sub line Playbook の output_schema」 + [revisions.md](revisions.md) line 811
**関連**: [phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) line 230 周辺 (実装ステップ詳述)

---

## 1. 背景

子 Playbook (sub line で動作する Playbook) は親に結果を返す責務がある。Phase 3 で導入された機構:

- `report_template` フィールドで機械的に生成 (LLM コール不要)
- LLM ノードの `output_schema['report_to_parent']` で構造化出力として生成

どちらかが書かれていれば親 ライン に結果が伝わる。しかし **書き忘れ** の検出が現状は **警告ログのみ** (例外を出さない) で、運用中に気付きづらい。

意図: 子として呼ばれうる Playbook (= `can_run_as_child=true`) が `report_to_parent` を欠いていたら **ロード時に raise** する。

旧名 `can_run_in_sub_line` は v0.11 で `can_run_as_child` に改名予定だったが、実装は未着手のまま残っている。

---

## 2. 現状

- `PlaybookSchema` (`sea/playbook_models.py`) に `can_run_as_child: bool` フィールドが **無い**
- runtime ルーティング: 警告ログのみ、例外は出さない
- `report_template` は実装済み、`output_schema['report_to_parent']` も実装済みだが、必須化されていない

---

## 3. 実装ステップ

### 3.1 `PlaybookSchema` への `can_run_as_child` 追加

`sea/playbook_models.py`:

```python
class PlaybookSchema(BaseModel):
    ...
    can_run_as_child: bool = Field(
        default=False,
        description=(
            "True if this Playbook can be invoked as a child sub-playbook "
            "(via subplay node or /run_playbook spell). When True, the Playbook "
            "must produce report_to_parent — either via report_template or via "
            "an LLM node whose output_schema includes report_to_parent."
        ),
    )
```

### 3.2 ロード時バリデーション

`PlaybookSchema` の Pydantic validator として:

```python
@model_validator(mode="after")
def _check_report_to_parent_required(self):
    if not self.can_run_as_child:
        return self
    has_report_template = bool(getattr(self, "report_template", None))
    has_output_schema_field = (
        # LLM ノードの output_schema いずれかに report_to_parent があれば OK
        any(
            isinstance(n, LLMNodeDef)
            and n.output_schema
            and "report_to_parent" in n.output_schema.get("properties", {})
            for n in self.nodes
        )
    )
    if not (has_report_template or has_output_schema_field):
        raise ValueError(
            f"Playbook '{self.name}' has can_run_as_child=true but neither "
            f"report_template nor any LLM node's output_schema includes 'report_to_parent'"
        )
    return self
```

(`output_schema` の正確な構造は `LLMNodeDef` の定義を確認して合わせる。`response_schema` フィールドかもしれない。)

### 3.3 既存子 Playbook への `can_run_as_child=true` 付与

子 Playbook (subplay ノード で参照される / `/run_playbook` で呼ばれる) を洗い出して true を付ける:

候補 (要洗い出し):
- `memory_recall_playbook.json` — `/run_playbook(name="memory_recall_playbook")`
- `spell_args_decider.json` — pre_spells 経路で起動
- `generate_image_playbook.json` — `/run_playbook` 経由

洗い出しコマンド:

```bash
# subplay ノード参照
grep -rln '"type":\s*"subplay"' builtin_data/playbooks/

# /run_playbook 参照
grep -rln 'run_playbook(name=' builtin_data/playbooks/
```

各 Playbook の JSON に:

```json
{
  "name": "...",
  "can_run_as_child": true,
  ...
  "report_template": "...",  // OR LLM ノードに report_to_parent を含める
  ...
}
```

### 3.4 旧コード (警告ログ) を例外に格上げ

runtime ルーティング側で警告ログを出していた箇所を削除 (or raise に置換):

- `sea/runtime_runner.py` / `sea/runtime_engine.py` 周辺で `report_to_parent` 不在時に WARNING 出力している箇所
- ロード時バリデーションが効くなら、ランタイムでの再チェックは不要

---

## 4. 完了条件

- `PlaybookSchema.can_run_as_child` フィールド追加済
- ロード時バリデーション実装: 不適合 Playbook は例外発生
- 既存子 Playbook に `can_run_as_child=true` 付与済 + 全部ロード成功
- pytest 全体 pass
- ランタイムの警告ログ経路は削除 (または例外に格上げ)

---

## 5. 関連リソース

- [02_mechanics.md](02_mechanics.md) line 533 — 「`can_run_as_child=true` の Playbook は `report_to_parent` を含む必要がある」
- [revisions.md](revisions.md) line 811 — バリデーション厳密化計画
- [phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) line 230 周辺 — 実装ステップ
- [phases/sub_line_playbook_sample.md](phases/sub_line_playbook_sample.md) — 子 Playbook の output_schema サンプル
- `sea/playbook_models.py` — `PlaybookSchema` 定義
- `sea/runtime_runner.py` / `sea/runtime_engine.py` — 警告ログ経路
- `builtin_data/playbooks/public/*.json` — 既存 Playbook ファイル群

---

## 6. 注意点

- 既存運用中の Playbook で警告ログのまま放置されているものがある可能性。例外格上げ時に **ロード失敗で起動できない** という事故が起きうる
- 推奨: 例外を出す前に、既存全 Playbook をロードして警告が出るものを洗い出し → そっちを先に直す → 例外格上げ
- `can_run_as_child=false` (default) の Playbook はバリデーション対象外なので、既存ペルソナの動作には影響しない (= 後方互換)
