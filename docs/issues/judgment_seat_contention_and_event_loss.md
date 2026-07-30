# 判断点の席の競合制御と、イベントの取りこぼし

**発見**: 2026-07-30（[判断プロンプトの静的一覧を head へ](judgment_static_lists_to_head.md) の Codex レビュー五巡目。移設の範囲外として切り出し）
**状態**: 未着手（設計判断が要る。まはー裁定待ち）
**関連**: `docs/intent/execution_ledger.md`、`docs/overview/audit_remediation_plan.md`（実行台帳 W1〜）、`saiverse/judgment_points.py` / `saiverse/autonomy_wiring.py` / `saiverse/execution_ledger.py`

## なぜ切り出したか

判断プロンプトの一覧を head へ移す作業のレビューで出てきたが、**中身は実行台帳の競合制御と回復**で、移設とは別の機構。ここで続けると一覧の移設が台帳の設計監査に化けたまま終わらない。移設側で閉じたのは「自分が作った穴」までで、以下は移設前から在るもの（③だけは移設の対処が新しく作った経路）。

## ① 席取りが CAS でなく、同じ判断を二重に開始しうる

`ExecutionLedger.claim_execution` は既存の prepared 行を再利用するため、**ほぼ同時の二重 claim には同じ `execution_id` を両方へ runnable として返す**。勝者を一意にするための条件付き遷移が `try_mark_running`（status=prepared のときだけ running）で、docstring にも「勝者の一意化」と明記されている。

ところが `run_judgment_point` は `mark_running`（無条件遷移）を呼んでいる。両者が prepared を観測すると**両方が `submit_meta_judgment` へ進みうる** — 有料 LLM 呼び出しと finalize の適用が二重になる。

- 直し: `try_mark_running` へ置き換え、False の側は台帳を触らず離脱する。
- 要検討: 敗者の結末は `duplicate` か `indeterminate` か（呼び出し側が代替経路を走らせてよいかが変わる）。

## ② 発火側の早期離脱が、勝者の running 台帳を failed に壊す

`fire_judgment_point` は claim の後、precondition の失敗と day_open / day_close の境界失敗で `_safe_mark_failed`（= `mark_failed`）を呼ぶ。`mark_failed` は running からの遷移も許すため、**別の claimant が既に running へ進んだ後にこれが走ると、勝者の台帳を上書きする**。勝者はそのまま LLM を実行中で、finalize の applied 遷移が失敗して結果の証跡を失う。

`run_judgment_point` 側は 2026-07-30 に `abandon_prepared`（prepared 限定 CAS）へ寄せたが、**入口側は mark_failed のまま**で非対称。

- 直し: pre-dispatch の離脱を全部 `abandon_prepared` に統一する（precondition / ライフ境界を含む）。

## ③ 代替経路を止めた結果、イベントが永久に消えうる

移設側の対処で入れた経路。席を放棄できなかった判断（別 claimant 所有 / 台帳が応答しない）では、`handle_external_event` は direct dispatch を**行わない**（二重応対を避けるため）。したがって prepared 回収だけが唯一の処理経路になる。

ところが回収側は、refire が `submitted=False` を返すと残っている prepared 行を**その場で failed に終端化**し、direct dispatch はしない。台帳障害のあとの refire 時に「自律 OFF / Playbook 欠如 / persona・pulse_controller 不在 / 再度の args 構築失敗」のどれかが起きると、**元のイベントは判断にも応対にも届かず消える**。一時障害がユーザーに見えるイベントの欠落として確定する。

### 裁定が要る点

**二重応対とイベント消失のどちらを取るか。** 既存コードの明示的な答えは「二重応対の方が害が大きい」（`handle_external_event` の unknown_reaction 分岐のコメント）で、現状はそれに揃えてある。消失は台帳が応答しないときに限られるが、限られていても消える。

選択肢:

- **A. 現状維持** — 消失を受け入れ、台帳障害を運用で検知する（ログ・監視）。
- **B. 回収側に backoff を入れる** — 一度の非 submission で終端化せず保持し、条件が直れば処理される。消失は減るが、prepared が長く残る。
- **C. 元イベントから durable な代替応対を確定できる回復経路を作る** — 一番正しいが、イベント本文を台帳の payload として持ち回す設計が要る。

## 実装時の注意

- ①②③は同じ関数群に触るので、まとめて一度に直すのが安全（片方だけ直すと非対称が残る）。
- 競合テストは 2 セッションを同期させる形が要る（片方だけが submit へ進むこと、敗者が台帳を触らないこと）。
- ③は回復 tick（`saiverse/execution_ledger_wiring.py` 系）まで届く。
