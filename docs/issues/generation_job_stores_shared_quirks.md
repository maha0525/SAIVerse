# ジョブの器 (Chronicle 生成 / スルース採取) に共通する癖

**状態**: 一部実装・検証待ち (2026-10-02)。癖 2 の冪等応答は実装済み・レビュー待ち。癖 1 (完了ジョブの保持) と癖 3 (開始時の状態競合) は未解決。保持期限・掃除方針は未決定。
**発見の経緯**: v0.3.11 のレビューで、採取ジョブの器 (`api/routes/people/sluice.py`)
へのローカル LLM の指摘 2 件を裏取りしたところ、手本にした Chronicle 生成の器
(`api/routes/people/arasuji.py`) にも同じ形があると分かった。この変更で持ち込まれた
欠陥ではなく、器の型ごと写った既存の癖。

## 癖 1: 完了したジョブがプロセス内の一覧に残り続ける

**未解決・今回の対象外**。以下の掃除案は採用しておらず、保持期限も定めない。

`_generation_jobs` (arasuji.py) と `_capture_jobs` (sluice.py) はプロセス内の辞書で、
ジョブが完了・失敗・中止になっても行を消す処理が無い。サーバーを長く起動したままに
すると完了済みの行が溜まり続ける。1 行は小さく、ジョブはユーザー操作でしか増えない
ので、実害になるのは相当長い運転だけ。

直すなら: 終端状態になってから一定時間 (ポーリングの読み残しを考えて例えば 1 時間)
経った行を、新しいジョブの作成時に掃く。両ファイルへ同時に入れる
(片方だけ直すと器の型が割れる)。

## 癖 2: 中止中のジョブへもう一度中止を送ると「Job is not running」と返る

修正前は cancel の状態検査が `("pending", "running")` (arasuji 側は + "started") のみで、
`"cancelling"` を含まなかった。既に中止要求済みのジョブへもう一度 cancel を押すと
`{"cancelled": False, "reason": "Job is not running"}` が返り、「実行されていない」と
誤読できる文面になっていた。実害は文面だけ (フラグは単調なので二重要求自体は無害)。

対応方針: `"cancelling"` は「既に中止要求済み」を意味する応答 (例:
`{"cancelled": True}` の冪等応答) にする。両ファイル同時。

### 実装と検証の範囲 (2026-10-02、レビュー待ち)

- 両 cancel API の既存ロック内で、ジョブの存在・所有 persona を確認した後に
  `cancelling` を受理済みとして返す。再要求では行・フラグ・進捗を更新せず、
  新しいジョブや worker も起動しない。
- `cancelled: true` は中止要求の受理であって停止完了ではない。worker が読む
  `cancel_requested` の単調性、チャンク間の中止、ポーリングが示す実際の終端は
  従来どおり。要求受理後でも worker が処理を最後まで書き切り `completed` になる場合がある。`completed` / `failed` / `cancelled` への要求は従来の false 応答を保つ。
- 新規回帰 `tests/test_generation_cancel_api.py` は合成ジョブと隔離
  `SAIVERSE_HOME` を使い、FastAPI の実ルートへ初回・再送・終端・不在・別 persona
  の中止要求を送り、ポーリングと行の不変性も確認する。LLM・本番データは使わない。
- 未検証の境界はブラウザ操作と本番 worker の実走。既存の実行処理は変更しない。
  癖 1 の行の蓄積は残るため、issue 全体は解決扱い・archive 移動にしない。

### 経緯

- 2026-10-02: 修正前の API 回帰で、Chronicle 3 状態・Sluice 2 状態からの
  二度目の中止だけが失敗することを確認 (5 失敗・29 合格)。癖 2 だけを
  両 API に同じ形で実装し、保持方針を要する癖 1 は切り離した。
- 同日: 新規 API 回帰 34 ケースを含む関連 5 ファイルで 288 件 + 61 subtests 合格。
  対象は `test_generation_cancel_api` / `test_arasuji_generation_status_mapping` /
  `test_sluice_capture` / `test_sluice` / `test_sluice_cold_isolation`。変更 Python の
  ruff、台帳検査、差分の空白検査も合格。参照文書の生成を実行し API・DB は差分なし。
  tool-catalog はこの隔離環境にないアドオン分が消えるため、その無関係な差分を除外した。
- 旧状態行: 「未解決 (2026-09-09 起票、実害は小さく後回し)」。

## 癖 3: 開始直前の中止表示を worker が running で上書きしうる

**未解決・今回の対象外**。`arasuji.py:_run_generation_job` と `sluice.py:_run_capture_job` は、`token.is_cancelled()` の確認と `_update_job(..., status="running")` を別の操作で行う。その間に cancel API が `cancel_requested=True` / `status="cancelling"` を保存すると、後続の状態更新で表示だけが `running` に戻る。フラグは単調に残るので、中止要求自体が消えたことを意味しない。token の確認と状態遷移の原子化は別件で、今回の冪等応答・UI 修正では触らない。

### UI の同族修正と検証 (2026-10-03)

[PR #355 レビュー](https://github.com/maha0525/SAIVerse/pull/355#issuecomment-5964529469) を実コードで裏取りした。生成ヘッダは `running` だけ、生成確認窓は畳めるかだけを見ており、補修の四状態判定と食い違っていた。`ArasujiViewer` の `isGenerationBusy` を共有し、`running` / `started` / `pending` / `cancelling` の間は生成ヘッダ・補修案内・両確認窓の開始を無効化する。両開始ハンドラーも同じ規則で POST を止める。終端・ジョブなしでは従来の畳み可能判定・補修見積もりに戻す。

- `npm run test:arasuji-busy`: 実 TSX とハンドラーを合成 hook/API で通す 23 ケース。四状態、確認窓を開いた後の最新ジョブ応答、両開始ハンドラー、終端後の再開、ポーリング遷移、畳めない状態、閉じて再開、失敗後の再試行を確認。
- `npm test` / TypeScript / TSX lint (エラー 0、既存警告 8) と、隔離した cancel API / 状態変換の Python 回帰 46 件は合格。
- サーバー側の同一ペルソナの重複開始拒否は追加しない。別画面・直接 API・ジョブをまだ観測していない開始前の時間差は、この表示側のガードだけでは防げない。実ブラウザ・実 worker / LLM は動かしていない。

直接原因は同じジョブに対する開始判定の重複・不一致、判断上の原因は cancel 応答だけで操作の一往復を閉じたと考えたこと、検査の穴は API 回帰だけで確認窓が開いたままの状態変化を検証しなかったこと。共有判定と境界をまたぐ状態遷移回帰で補う。この検査軸はアップロード中の再開始や保存中の確認窓にも適用できるが、それらの実装変更は本件に含めない。

- 台帳から移送した旧次アクション: 「中止中への再要求を受理済みとして返す修正はレビュー待ち。次 = 両 API の回帰と差分をレビューする。完了ジョブの保持方針は未決定で、本件では変更しない。」

- 2026-10-03: 正規の `npm run build` (Turbopack) も隔離先指定で合格。別途試した webpack ビルドは既存 `page.module.css` の global-only selector を拒否したため不合格（未変更 CSS）。共有依存への symlink はビルド用のローカルコピーに置き換え、追跡ソースは変えていない。
