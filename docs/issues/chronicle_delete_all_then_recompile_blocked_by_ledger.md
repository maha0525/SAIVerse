# Chronicle 全削除の直後の再編纂が、実行台帳の completed 行に塞がれる

**発見**: 2026-09-08、テリス (persona_3_city_a) での全量再編纂テスト中 (実機)。

## 症状

「Chronicle 全削除」→「過去の会話をあらすじにする」(補修) の順に操作すると、補修が
即座に「別のあらすじ処理が同じ範囲を処理中または処理済みです。しばらく待って再実行
してください。」で失敗する。待っても直らない — 新しいメッセージが増えるまで永久に
同じエラーになる。

## 実機の時系列 (ログ: `~/.saiverse/user_data/logs/20260908_121554/backend.log`)

1. 13:03:51 補修 #1 が claim (`[ledger] claimed ... kind=metabolism.run key=persona_3_city_a:79c27f66-...`) → 1 時間 42 分走って 14:45:50 に completed (約 100 チャンク確定)。
2. 17:37:46 ユーザーが UI で Chronicle 全削除 (+ 17:37:47 Memopedia 全削除)。
3. 17:37:55 補修 #3 → 即 `window_claimed`。

## 原因

- 実行台帳 (`saiverse/execution_ledger.py`) の claim 鍵は
  `persona_id:編纂範囲の末尾メッセージ ID` (`sea/session_lifecycle.py` の
  `claim_key = f"...:{_window_end_id}"`)。completed 行は同じ鍵の再 claim を
  ブロックする (「既に走った」— 二重 LLM コストの防止)。
- この規則は「編纂が進めば範囲の末尾が変わり、鍵は自然に前進する」前提。
  **全削除は成果物だけ消して台帳を消さない**ため、削除後の再編纂は #1 と同じ鍵に
  なり、completed 行に正面から当たる。
- 台帳が守ろうとした「同じ範囲を二度 LLM にかけない」は、成果物が存在する限り
  正しい。全削除の後は成果物が無く、再実行はユーザーの意図そのもの — 保護の前提
  (arasuji の source_ids による冪等スキップが安全網) も成果物ごと消えている。

## 修正の方向 (未着手)

- **削除の境界が台帳の整合も持つ**: Chronicle 全削除ルート
  (`api/routes/people/arasuji.py` `delete_all_arasuji_entries`) が、その persona の
  `kind=metabolism.run` の行の鍵を退避する (`{key}#deleted-{...}` — failed /
  unknown 照合と同じ退避の形。行の削除ではなく退避なので、走った事実の記録は残る)。
  個別削除は対象外 (範囲の末尾が変わらない限り衝突しない)。
- 文言も直す: 「しばらく待って再実行してください」は、この形では嘘になる
  (待っても解けない)。claim 競合の文面は既知の改善案件 (稟乃さんの
  「メッセージが死ぬほどわかりづらい」の束) と同時に見直す。

## 応急処置 (ユーザー向け)

そのペルソナに一言話しかける (新しいメッセージで範囲の末尾が変わり、鍵が変わる)
→ 補修を再実行。

## 関連

- `docs/intent/execution_ledger.md` §「metabolism.run の期限と unknown の規則 (2026-09-03)」
  (unknown の照合による鍵退避 — 同じ退避機構を completed × 削除にも使う)
- `docs/intent/chronicle_coverage_gaps.md` (この検証の発端)
