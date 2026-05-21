# Issue: native tool の 4-tuple 戻り値が SEA runtime で正しく展開されない

**ステータス**: ✅ 修正実装済 (= 実機検証 (see.py multimodal) 待ち)
**優先度**: medium (= 機能的影響あり、 ただし当面の workaround で運用可能)
**作成日**: 2026-05-19
**修正日**: 2026-05-21
**関連**:
- `tools/core.py` (= `parse_tool_result()` 正規 normalizer)
- `sea/runtime_llm.py:_run_spell_tool_async` (= 4-tuple 未対応の bug 元)
- `expansion_data/saiverse-stackchan-addon/tools/see.py` (= 4-tuple return、 影響を受ける tool の代表例)

## 観測

2026-05-19 に env3 (温湿度取得) tool を実装中、 4-tuple `(text, ToolResult, file_path, metadata)` を return したら **LLM 側に tuple repr 文字列がそのまま渡る** 事態に。

ペルソナが見た spell 結果テキスト:
```
('温度: 32.3°C、湿度: 46.4% (ENV III / SHT30、Stack-chan Port A)。', ToolResult(history_snippet='温度: 32.3°C、湿度: 46.4% (ENV III / SHT30、Stack-chan Port A)。'), None, None)
```

期待値は単なる:
```
温度: 32.3°C、湿度: 46.4% (ENV III / SHT30、Stack-chan Port A)。
```

## 根本原因

SEA runtime の spell tool 呼び出しは `sea/runtime_llm.py:_run_spell_tool_async` (line 505-) で行われる。 戻り値の normalize は line 566-575:

```python
# Normalize to (text, metadata). Tools may return:
# - str → (str, None)
# - (str, dict) → as-is
# - other → (str(x), None)
if isinstance(raw_result, tuple) and len(raw_result) >= 2 and isinstance(raw_result[1], dict):
    result_str = str(raw_result[0])
    result_metadata: Optional[Dict[str, Any]] = raw_result[1]
else:
    result_str = str(raw_result)
    result_metadata = None
```

つまり SEA runtime は:
- `str` → `(str, None)`
- `(str, dict)` の 2-tuple → そのまま
- それ以外 → 全体を `str()` 化

しか想定していない。 4-tuple `(text, ToolResult, file_path, metadata)` は `raw_result[1]` が `ToolResult` (= 非 dict) のため else 分岐に落ちて **`str(raw_result)` で tuple 全体が repr 文字列化** されて LLM に渡る。

一方、 `tools/core.py:parse_tool_result()` は 4-tuple を正しく展開する **正規 normalizer**:

```python
def parse_tool_result(res: Any) -> Tuple[str, Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    # ...
    if isinstance(res, tuple):
        if len(res) == 2: ...
        if len(res) >= 3:
            content = str(res[0])
            snip = res[1]
            file_path = res[2]
            if isinstance(snip, ToolResult):
                snip = snip.history_snippet
            if file_path is not None:
                file_path = str(file_path)
            if len(res) >= 4 and isinstance(res[3], dict):
                metadata = res[3]
            return content, snip, file_path, metadata
```

つまり SAIVerse には 2 系統の normalizer が共存していて、 spell 経路は 4-tuple 非対応の方を使ってる。 `parse_tool_result()` は chat 経路 or 別経路で使われていると思われる (= 要確認)。

## 影響を受ける tool

`expansion_data/saiverse-stackchan-addon/tools/see.py` が代表例。 戻り値:
```python
return text, ToolResult(history_snippet=snippet), stored_path.as_posix(), metadata
```

ペルソナが `see` を spell として叩いた時:
- 期待: LLM が画像 attachment (`metadata['media']`) を受け取って multimodal で見る
- 実際: LLM は tuple repr 文字列を読む。 中に画像 markdown (`![見えた光景](.../image.jpg)`) は含まれるので、 ペルソナは「画像のパス文字列」 から「画像を見た」 と推測している可能性

→ ペルソナが「画像が見えた」 と語っていても、 実際の multimodal attachment 経路は動いていない疑惑。 要検証。

他に 4-tuple を返す可能性のある tool:
- `builtin_data/tools/*.py` で `parse_tool_result()` 想定の戻り値型を返してる tool
- 未調査 (= 本 issue 解消時に grep + 影響範囲確認要)

## 当面の workaround

新規 native tool は **`str` または `(str, dict)` の 2-tuple** を return する:

```python
# 推奨 (シンプル、 attachment 不要):
def my_tool() -> str:
    return "結果テキスト"

# multimodal attachment が要る場合:
def my_tool() -> Tuple[str, Dict[str, Any]]:
    return "結果テキスト", {"media": [{"type": "image", "path": "..."}]}
```

env3 (本 issue 発見の契機) は str-return に倒して回避済 (2026-05-19、
`expansion_data/saiverse-stackchan-addon/tools/units/env3.py`)。

## 修正方針 (案)

**(B-1) SEA runtime に `parse_tool_result()` を統合**:
- `_run_spell_tool_async` の line 566-575 の normalize 処理を `parse_tool_result()` の呼び出しに置き換える
- 結果として 4-tuple が正しく `(content, snippet, file_path, metadata)` に展開される
- `snippet` (= history_snippet)、 `file_path`、 `metadata` を spell-result message にどう乗せるかは設計確認要 (= 既存 `metadata['media']` 経由の multimodal attachment 経路をそのまま使う)

**(B-2) 戻り値型の規約を明文化 + 既存 tool を migrate**:
- 「native tool は `str` or `(str, dict)` 」 を documented 規約に
- 既存 4-tuple tool (`see.py` 等) を `(str, dict)` に migrate
- 強化版 normalizer 不要、 シンプル維持

(B-1) は影響範囲が小さいが SEA runtime のテスト要、 (B-2) は規約変更で既存 tool 全部の見直し要。 どっちが筋かは要議論。

## 検証シナリオ (本 issue 修正後)

1. **see で画像 attachment が LLM の multimodal 経路で届くか**
   - 期待: LLM (Gemini / Claude) が「実際の画像内容」 に基づく応答 (= 文字列 path からは推測できない specifics)
   - 現状: LLM が tuple repr の中の markdown を見て、 path 文字列から間接推測
2. **env3 で history_snippet が SAIMemory に記録されるか**
   - 期待: 会話ログに「温度: 32.3°C、...」 が snippet として残る
   - 現状: snippet 経路が機能してない (= ToolResult が無視されてる)

## ログ

- 2026-05-19: env3 spell 実装中に発見。 当面 env3 は str-return で回避。 see.py 等の 4-tuple tool は壊れた状態のまま (= ペルソナの「画像見えた」 が本当に multimodal で届いてるか要再検証)。 本 issue 作成。
- 2026-05-21: (B-1) で実装。 `sea/runtime_llm.py:_run_spell_tool_async` の normalize を `tools.core.parse_tool_result` 呼び出しに置換。 ただし `parse_tool_result` の 2-tuple branch が `(text, snippet)` 扱い (= chat 経路想定) で、 spell 経路の慣習 `(text, dict_metadata)` (`run_playbook` の forwarded_metadata 等) と非互換だったため、 `parse_tool_result` 自体も拡張 (2-tuple の 2nd 要素が dict なら metadata、 ToolResult なら snippet、 それ以外なら snippet として扱う)。
  - 副次発見: snippet 経路は実質 dead path。 LLM クライアント側 (gemini/openai/nvidia_nim/xai) に `history_snippets` パラメータ受け口は存在するが、 渡している実コードが無い (= テスト test_llm_clients.py:551 のみ)。 SAIMemory に snippet を乗せる経路を実装する場合は別 issue 切り出しが筋。
  - 回帰テスト: `tests/test_run_spell_tool_async_return_normalization.py` 追加 (7 ケース、 str / (str,dict) / (str,ToolResult,path) / 4-tuple / ToolResult-only / unknown tool / 例外)。
  - 実機検証 (検証シナリオ 1: see.py で multimodal attachment が届くか) は Stack-chan 実機が要るので別途実施。
