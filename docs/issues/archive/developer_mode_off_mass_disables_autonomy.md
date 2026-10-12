# 開発者モード OFF が全ペルソナの自律行動を一括 OFF にする

**起票**: 2026-09-01 (亡霊トグル「自律会話モード」撤去作業中に発見)
**状態**: 解決 (2026-10-12 — 連動を撤去。代替はグローバル自律全停止)

## 現象

`POST /api/config/developer-mode` で OFF にすると、副作用として
**全ペルソナの `AUTONOMY_ENABLED` が False に一括更新される**
(api/routes/config.py の set_developer_mode)。

## 問い

この連動は ConversationManager 時代の「開発者モード = 自律実験モード」という
前提の名残に見える。自律行動 v2 では AUTONOMY_ENABLED はペルソナ個別の
恒常設定であり、開発者モードの表示切り替えが世界全体の自律を止めるのは
釣り合わない可能性がある。まはーの裁定を得てから、連動の撤去か維持を決める。

ONに戻しても各ペルソナの AUTONOMY_ENABLED は自動では復元されない
(一括 OFF は不可逆) 点も判断材料。

## 決着 (2026-10-12)

まはーの裁定 ([v0.4 計画](../../intent/autonomous_behavior_v04_plan.md) §4 決定 12) により、
連動を撤去した。`set_developer_mode` は `manager.state.developer_mode` を切り替える
だけになり、開発者モードを OFF にしてもペルソナの `AUTONOMY_ENABLED` は書き換わらない。

「誰が動いているか分からない場面で、自発の課金を一括で止める」という用途は、
**グローバル自律全停止** (`GET` / `POST /api/config/autonomy-all-stop`、実体は
`saiverse.autonomy_wiring.set_autonomy_all_stop`) が引き継ぐ。全停止はペルソナごとの
設定値を書き換えないので、解除すれば元の設定で再開する (旧連動の「ON に戻しても
復元されない」不可逆性はこれで消えた)。
