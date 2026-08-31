# W6 走行メモ — head の fail-closed 化 (SEA 監査 S6)

> セッション固有の走行メモ (2026-07-21)。工程の真実は [audit_remediation_plan.md](../overview/audit_remediation_plan.md)、恒久的な設計正典は [cached_head_architecture.md](../intent/cached_head_architecture.md) §6 C7 が持つ。

## 患部 (監査 S6 の要約)

- `capture_all` が Section capture 例外を握り、既存値が無ければ **value=None のまま fresh snapshot を作る** → `ensure_snapshot` は key 集合しか見ず None を欠損と認識しない → render は None を黙って skip → **persona prompt / core memory を欠いた head で LLM が走り、人格に属さない発話が本人履歴へ確定する**。
- head pipeline 全体の例外も `prepare_context` が exception log だけで握って LLM に進む。
- snapshot の DB 保存失敗も内部で握る → Metabolism 直後の保存失敗 + 再起動で **旧 head が fresh TTL 中の snapshot として復元** (system prompt 編集・記憶整理のロールバックが TTL 失効まで自己修復しない)。

## 確定設計 (Fable 裁定)

1. **required Section** = `required = True` 宣言 (common_prompt / persona_self / core_memory — 人格の同一性を担う 3 つ)。「空を render する」のは正規 (空の本人)、fail-closed の対象は**失敗**のみ。
2. **capture 失敗の痕跡は None でなく欠損**: 既存値あり → 据え置き (stale-but-real)。無し → key 省略 + `LineHeadSnapshot.capture_failures` に理由。`ensure_snapshot` は None 値も欠損として認識し、**欠損分だけ** `recapture_missing` で毎呼び出し再試行 (= 復旧後の自己修復)。
3. **readiness 検証** (`render_head_messages`): pin した snapshot に対し「required ∩ enabled が実体値を持つ」+「pin 版までの保存確認 (`ensure_persisted`)」を検証、破れは `HeadNotReadyError` (stage=capture/persist)。render 例外も required は raise (stage=render)。
4. **store.save は commit 成否を返す**: DB commit 失敗 / required の serialize 失敗 / required の実体値欠損は **DB に書き込まず** False。旧版 upsert は**版条件付き UPDATE 一文**で拒否。
5. **prepare_context**: `HeadNotReadyError` は伝播。head pipeline の想定外例外も `reqs.system_prompt` 時は `HeadNotReadyError` に包んで raise (optional-only の head は degrade)。
6. **core_memory の内部握り撤去**: DB 読み失敗で空を返す旧実装は「コア記憶ゼロの本人」の捏造 → raise に変更 (conn 不在 = 構造的不在のみ空)。

### 中断の受け皿 (fail した Pulse はどうなるか)

- 会話 Pulse (`run_playbook` → `_prepare_context`): 例外が `run_meta_user` → PulseController → API へ伝播し正直なエラー。ユーザー入力の取り込みは cursor 冪等 (W5 M8) なので再試行安全。
- 判断点 / 作業コマ / schedule: 実行台帳の failed 行 (W1〜W3 の再試行規律に乗る)。
- gold_panning / keepalive: 既存の失敗隔離 (try/except) で degrade。

## Codex レビュー 4 巡 8 件消し込み (受諾 8 / 却下 0)

| 巡 | 指摘 | 対処 |
|---|---|---|
| 1-P1 | persist_failed フラグが版を無視 — 並行 capture で旧版の保存成否が新版の状態を上書きし fail-closed 迂回 | フラグ廃止 → `persisted_version` (保存確認済み版) の単調前進 |
| 1-P2 | 欠損再 capture が capture_all — 1 Section の持続故障で毎 Pulse 全 head 再構築 (= cache 破壊) + B 全リセット | `recapture_missing` 新設 (欠損分のみ、全滅時は版据え置き) |
| 2-P1a | readiness 検証と render の間の TOCTOU (未保存の新版を render) | snapshot を pin し検証・persist 確認・render・composition を同一オブジェクトで |
| 2-P1b | 遅延した旧版 save が新版 commit を上書き (DB 巻き戻り) | store.save に版ガード + 採番を DB 版から継続 (`_stored_base_version`) |
| 2-P2a | required serialize 失敗時に欠損行を commit してから False (復旧元破壊) | DB 書き込み前に拒否 |
| 2-P2b | recapture_missing がロック外 capture 間の並行 fill を古値で上書き | merge 時に「今も欠損している名前」だけ適用 |
| 3-P1 | 採番と公開が別ロック区間 → 同版番号の異内容 snapshot / SELECT→UPDATE の check-then-act | 採番を公開ロック内へ + persist 記帳を snapshot 同一性 (is) に結合 + 版条件付き UPDATE 一文 |
| 3-P2 | required の capture 失敗で key ごと省かれた snapshot は serialize 検査を通らず保存成功 → 完全な既存 DB 行を欠損行で上書き | 保存前に required の実体値欠損も拒否 |
| 4-P1 | `load_version` 一時失敗を「行なし」と同一視 → v1 採番が DB の vN に恒久 stale 拒否され fail-closed が解けない | `_try_rebase_stale_version` — stale 拒否時に DB 版を読み直し DB+1 へ再採番して一度だけ再保存 (自己回復) |

### 5 巡目 = 裁定却下 1 件 (受諾 9 / 却下 1 で打ち切り)

指摘: 「pin した版自体の保存成否を確認すべき (並行 capture の新版保存で未保存の pin 版が通る / 逆に保存済み pin 版が新版未保存で不要停止する)」。裁定:

- **不要停止の主張は現行コードで起きない** — `ensure_persisted` は最初に `persisted_version >= pin版` を検査するので、pin 版が保存済みなら新版の状態に関係なく即通過する (head 実装で裏取り)。
- **「未保存 pin 版を新版の durable で通す」は仕様として引き受ける** — 不変条件は「durable 版 >= 描画版」の**単調性** (restart が旧 head へ巻き戻らないこと = 監査 S6 の害の定義) であって版の厳密一致ではない。厳密一致は並行 capture のたびに正当な Pulse を止める劣化を買う。`ensure_persisted` docstring に明文化済み。

## 回帰

- 新設 `tests/test_head_fail_closed.py` 27 件: capture/render/persist × required/optional × 失敗/復旧、None=欠損、stale 版拒否、採番継続、版 pin、並行 fill 保護、再採番自己回復、prepare_context 伝播 (HeadNotReadyError / 汎用例外の格上げ / optional-only degrade)。
- 既存 head 系スイート + 本体スイート全緑。

## 引き受ける残差

- required の判定は「enabled_sections に required が含まれる呼び出し」に限る。`reqs.system_prompt=False` の呼び出し (sub-line の fork 等) は元々人格 head を積まない設計なので対象外。
- `save_last_notified` (B のみ) の失敗は fail-closed の対象外 (restart 後の再通知重複は自己限定的で人格の欠損ではない)。成否 bool は返す (観測用)。
- 会話 Pulse の中断はユーザーに一度エラーとして見える (LLM を人格なしで走らせるより正直な失敗を選ぶ、監査の修正方針どおり)。

## まはー実機検証の観点

1. 通常運転で挙動が変わらないこと (head 生成・キャッシュヒット・差分通知が従来どおり)。
2. `~/.saiverse/personas/<id>/memory.db` を一時的にロック/破損させた状態で会話 → ペルソナが「人格なしで返答」せず、エラーになること。復旧後の次の会話で自然に回復すること。
3. ログに `head_pipeline: rebasing snapshot version` / `refusing stale save` が平常運転で**出続けない**こと (出続ける = 版管理の想定外)。
