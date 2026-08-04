# LLM 使用量の記帳が経路ごとに欠落する

**状態**: 未解決 (2026-08-01 起票、Codex レビュー七〜八巡目)。使用量帰属の修正 (`92ead95`〜`41307d9`) の過程で、レビューが記帳の仕組み全体へ及んで見つかったもの。**個別の修正ではなく、記帳の置き場所そのものの設計課題**として立てる。

関連: [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md) の不変条件「使用量の帰属」 / [`usage_pricing_lookup_falls_back_to_api_name.md`](usage_pricing_lookup_falls_back_to_api_name.md)

## 芯

**使用量の記帳が「成功して戻ってきた場合」にだけ効く形で散らばっている。** API 呼び出しが成立した時点で課金は発生しているのに、その後の解釈・検証・retry の都合で記録が消える経路が複数ある。加えて、runtime を経由しない直接の LLM 呼び出しは最初から誰も記帳していない。

一箇所ずつ塞ぐと同じ形の穴が別経路で開き続けるので、**「応答を受け取ったら必ず一度記帳する」を持つ層**を決めるのが本題。

## 欠落している経路

### 1. 例外で終わると DB へ届かない

`sea/runtime_llm.py` の `_record_llm_usage` は `generate` が正常復帰した後にしか呼ばれない。client 側が `_latest_usage` に保存していても、抽出失敗・不正 JSON・content filter で例外になると記帳されずに捨てられる。

- NIM の forced function calling: レスポンスの usage は保存するが、choices 不在 / tool call 不在で例外
- Codex: `_finalize` 後の structured JSON parse 失敗

`41307d9` で client 側の保存位置は解釈より前へ移したが、**client が保持していても runtime が拾わない**構造は残っている。

### 2. 空レスポンスの retry が使用量を捨てる

`sea/runtime_llm.py` の通常 streaming / tool streaming の両方で、空応答を検出すると `consume_usage()` の結果を `discarded_usage` として破棄して retry する。API が prompt tokens を返していればその試行は課金対象になりうるが、記録に残らない。全試行が空なら全部消える。

「空応答は無料」と決めるなら provider ごとの根拠が要る。決めないなら試行ごとに記帳する。

### 3. runtime を通らない直接呼び出しが記帳されない

factory で client を作って `generate` するだけの箇所は、`consume_usage()` も `UsageTracker.record_usage()` も呼んでいない。外側の runtime が記録するのは tool を発行した親 client の usage だけなので、これらの呼び出しは丸ごと無記録になる。

確認されている箇所:

- `builtin_data/tools/get_since_last_user_conversation.py` (summary)
- `saiverse/media_summary.py` (複数箇所)
- `sai_memory/curation_ops.py`
- `sai_memory/memory/entity_extractor.py`
- CLI 群（`scripts/` 配下。`maintain_memopedia.py` は 2026-08-05 に削除済み）
- `saiverse/day_plan.py` `_choose_free_time_kind` (自由時間コマの種別選択、
  軽量構造化出力 1 発。時間割改修 T3 で追加 — Codex 時間割五巡目 #3 で検出。
  経路個別の ad-hoc 記帳はこの issue の反パターンなので足していない)

## 修正の方向 (設計裁定が要る)

1. **応答受領を単位にした記帳**: request context を持ち、成功・解析失敗・例外の全終了経路で一度だけ記帳する。client 側の `_latest_usage` を runtime が拾い損ねる構造をなくす。
2. **retry の扱いを決める**: 試行ごとに記帳して retry metadata を付けるか、「空応答は無料」を provider ごとの根拠つきで明示するか。
3. **standalone 呼び出しの共通 helper**: persona / building / category を受け取って記帳する口を一つ作り、summary・media・memory・CLI の直接 `generate` を全部そこに通す。

いずれも影響範囲が広く、記帳の粒度 (呼び出し単位か、試行単位か) と category の設計を伴うため、**まはーの裁定を待つ**。

## 経緯

`llm_clients/openai_codex.py` が `_store_usage(model=...)` で API 名を渡していた件 (`92ead95`) の Codex レビューが起点。八巡のあいだに、帰属 (どの名前に付けるか) の問題から記帳 (そもそも記録されるか) の問題へ対象が移り、後者が単発の修正では閉じない範囲であることが分かった。帰属側の修正と実害のあった取り違えは `41307d9` までで塞いである。

## 経緯: LLM 使用量の記帳が経路ごとに欠落する (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

**issue 起票 (2026-08-01、`6fbe984`)**。
使用量帰属の修正 (`92ead95`〜`41307d9`) の Codex レビュー七〜八巡目で、対象が「帰属 (どの名前に付けるか)」から「記帳 (そもそも記録されるか)」へ移り、単発の修正では閉じない範囲だと判明。
**芯 = 記帳が「成功して戻ってきた場合」にだけ効く形で散らばっている** — API 呼び出しが成立した時点で課金は発生しているのに、その後の解釈・検証・retry の都合で記録が消える。
欠落三経路: ①例外で終わると DB へ届かない (client が `_latest_usage` に保持していても runtime が拾わない構造。
NIM の forced function calling / Codex の structured JSON parse 失敗) ②空応答の retry が `consume_usage()` の結果を破棄 (prompt tokens は課金されうる・全試行が空なら全消失) ③runtime を通らない直接の `generate` が最初から無記帳 (summary tool / media_summary / curation_ops / entity_extractor / CLI 群)。
**修正の方向は三案** (応答受領を単位にした記帳 / retry の扱いを「試行ごと記帳」か「空は無料 (provider ごとの根拠つき)」で確定 / standalone 呼び出しの共通 helper) だが、いずれも記帳の粒度と category の設計を伴うため裁定が要る。
**帰属側の実害 (取り違え) は `41307d9` までで塞いである** — 本件は残る欠落の設計課題。
次 = まはーの裁定 (三案の方向決め)
