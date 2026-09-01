# 開発者モード OFF が全ペルソナの自律行動を一括 OFF にする

**起票**: 2026-09-01 (亡霊トグル「自律会話モード」撤去作業中に発見)
**状態**: 未解決 (裁定待ち — 現行の自律行動 v2 でこの連動が正しいか)

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
