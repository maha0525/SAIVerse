# Issue: `PersonaMixin` に AI 編集メソッドの死んだ複製が残り、保守され続けている

**ステータス**: 🔲 未着手
**優先度**: low (= 動作影響なし。ただし「保守コストを払い続けている dead code」なので放置すると増え続ける)
**作成日**: 2026-08-11
**関連**:
- `manager/persona.py:655` (`get_ai_details`) / `manager/persona.py:687` (`update_ai`) — 複製 (動かない)
- `manager/admin.py:1100` / `manager/admin.py:1158` — 本物
- `saiverse/saiverse_manager.py:2491` / `saiverse/saiverse_manager.py:2508` — `self.admin` への委譲
- `tests/test_mixin_host_contract.py` — `RuntimeService` の未解決参照を既知の負債として記録

## 背景

ワールドエディタのペルソナ編集フォームを支える `get_ai_details` / `update_ai` が、`manager/persona.py` (`PersonaMixin`) と `manager/admin.py` (`AdminService`) に二重に存在する。**動くのは `AdminService` 側だけ**で、`PersonaMixin` 側は上書きされて到達しない。

どの実装に解決されるかは読みではなく実行で確認した:

| クラス | `update_ai` / `get_ai_details` の解決先 |
|---|---|
| `SAIVerseManager` | 自前の定義 (中身は `self.admin` への委譲) |
| `AdminService` | 自前の定義 = **本物** |
| `RuntimeService` | `PersonaMixin` = **複製** |

`AdminService` も `RuntimeService` も `PersonaMixin` を継承しているが、`AdminService` は自前定義で上書きしている。API の入口 (`api/routes/world.py:425`、`api/routes/people/config.py:107` ほか) はすべて `SAIVerseManager` 経由なので、実際に走るのは本物だけ。

### なぜ放置してはいけないか

複製は「ただ残っている」のではなく、**本物と一緒に保守され続けている**。読んだ人には維持されている現役実装に見えるので、次に触る人も同じ手間を払う。

- `78e7ae22` (ACTIVITY_STATE 解体) — 複製側にも `autonomy_enabled` を追加
- `4aa694c0` (モデル設定の追加) — 複製側にも `lightweight_model` / `vision_model` を追加

一方で本物はさらに先へ進んでいて (`appearance_image_path` / `audio_model` / `video_model` / `memory_weave_model` / `autonomous_chronicle_enabled` / `auto_recall_enabled` / `memopedia_index_enabled` / `core_memory_char_budget` など)、複製は追随しきれていない。追いつく努力自体が無駄で、かつ追いつかないまま残ると誤読の材料になる。

## 撤去時の注意点

複製は「誰からも届かない」わけではない。`RuntimeService` だけが上書きせずに継承しているため、`RuntimeService.update_ai` / `.get_ai_details` は複製へ解決される。

- 現時点で `RuntimeService` 経由でこの 2 つを呼ぶコードは repo 内に存在しない (grep 確認済)
- したがって削除しても壊れないが、削除後は `RuntimeService.update_ai` が `AttributeError` になる
- 古い実装が黙って走るより明示的に落ちる方が望ましいと判断しているが、これは撤去の副作用として意識しておく

## 解決案候補

1. `manager/persona.py` から `get_ai_details` / `update_ai` を削除する (第一候補)
2. 併せて `AdminService` 側にテストを付け、本物の契約 (永続化と、複製が追随できていなかった新しめの項目) を固定する
3. `RuntimeService` が `PersonaMixin` から何を継承すべきかという、より広い線引きは `tests/test_mixin_host_contract.py` が扱う範囲。本 issue では踏み込まない

## テストの組み方 (前回の作業から回収したメモ)

2026-06-15 起点の作業用 worktree (`zealous-meninsky-d53f1b`) で一度書かれたが、`interaction_mode` 廃止前の signature に依存していて現行では動かないため、成果物は破棄した。再実装で使える形だけ残す:

- `AdminService.__new__(AdminService)` でインスタンスを作り、`update_ai` が実際に触る属性だけ注入する (`SessionLocal` / `personas` / `building_map` / `state` / `_set_persona_avatar`)。`personas` を空 dict にすると LLM クライアント再生成の経路を踏まずに済む
- DB は `create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})`。`StaticPool` により毎回の `SessionLocal()` が同一接続を見るので、`create_all` と後続クエリが噛み合い、`db.commit()` の往復を実物で通せる
- 押さえる観点: 主要項目の永続化 / 空文字のモデル指定が `None` になること / トグル系を省略したとき既存値が変わらないこと / 存在しない AI でエラーを返すこと / `IS_DISPATCHED` 中の `HOME_CITYID` 変更を拒否し DB を変更しないこと

## 検証

- `python -m pytest` フルスイートが引き続き緑
- ワールドエディタでペルソナを編集 → 保存 → 再読込で、モデル設定・表示設定・Chronicle 系トグルが往復すること

## ログ

- 2026-08-11: 作業用 worktree `zealous-meninsky-d53f1b` の未コミット変更 (複製の削除 + テスト追加) を確認する過程で issue 化。前提 (複製が死んでいること) は現行ツリーでも成立していることを実行で確認。worktree 自体は成果物が 2 か月古く再利用できないため、本 issue に知見を移送した上で撤去した
