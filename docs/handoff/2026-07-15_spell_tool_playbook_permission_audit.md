# Spell / Tool / Playbook 権限 一次監査

**開始日**: 2026-07-15

**状態**: 指摘あり・一次監査完了  
**監査基準**: `113567e`  
**監査軸**: visible一覧と実行時gateの一致、aspect権限、persona/contextvars、Playbook scope・city permission・credential、内部tool露出

## 現行Intentの再確認

確認対象:

- `docs/intent/persona_cognition/mode_spell_permissions.md`
- `docs/intent/persona_cognition/nested_subline_spell.md`
- `docs/intent/quick_spell.md`
- `docs/intent/mcp_addon_integration.md`
- `docs/intent/playbook_state_management.md`
- `docs/intent/persona_cognition/line_tag_responsibility.md` §10

主要な正典:

- Spell一覧は説明であり、権限は実行時gateで強制する。restricted mutatorはactive Aspectから許可を決める。
- Playbookのscope、developer-only、city permission、credential、router-callableは、一覧から隠すだけでなく実行入口で同じ判定を行う。
- persona/building/manager/pulse contextはcontextvarsで実行単位に伝播し、並列Pulseや別personaへ漏らさない。
- Playbook定義の引数とsystem stateを分離し、呼び出し元が許可されていないstate/toolへ暗黙アクセスできないようにする。

## Findings

### [P1] Playbook一覧のcity権限を`run_playbook`実行時に再検査せず、blocked/user_only/ask_every_timeを名前指定で起動できる

- 場所: `builtin_data/tools/list_available_playbooks.py:36-137`, `builtin_data/tools/run_playbook.py:45-225`, `sea/runtime.py:2052-2115`, `sea/runtime_engine.py:240-305`
- 事実:
  - `list_available_playbooks()` はscope・credential・cityの `PlaybookPermission` を確認し、`blocked` / `user_only`、auto時の`ask_every_time`を除外する。
  - `run_playbook()` は一覧を使わず `_load_playbook_for()` を名前指定で直接呼ぶ。またtext spell executorは自律Pulseでも `persona_context(auto_mode=False)` を張るため、`list_available_playbooks`をspellとして呼んだ場合のauto-mode filter自体も無効になる。
  - `_load_playbook_from_db()` が再検査するのはscopeと`dev_only`だけで、city permission・required_credentialsを見ない。
  - その後の`run_playbook()`は`router_callable`だけ確認してsub-lineを起動する。したがって、一覧に出ない名前をLLMが既知なら実行できる。
  - 通常のexec node側にも `user_only` のdeny分岐がなく、`blocked` と `ask_every_time` 以外は実行へfall throughする。UIの「今後使わない」が保存する `user_only` は自律実行を止めない。
- 影響:
  - ユーザーがcity設定で禁止・user-only化したPlaybookを、personaが過去の記憶や推測した名前から自律起動できる。
  - `ask_every_time`も`run_playbook`経由では確認dialogなしで実行され、ネット送信・外部生成・破壊的toolを含むPlaybookの承認境界が消える。
  - missing credential Playbookも起動段階では拒否されず、下流toolの実装次第で別persona/global credentialへfallbackする余地を作る。
- 修正方針:
  - Playbook authorizationを単一関数 `authorize_playbook_invocation(subject, playbook, origin, pulse_type)` に集約し、list・load・run_playbook・exec・schedule/UIの全入口で同じ結果を使う。
  - `user_only`はpersona/auto起動を常にdenyし、ユーザーが明示したUI起動だけ許可する。`ask_every_time`はinteractive承認tokenまたは明示的なschedule preapprovalを要求する。
  - credentialはpersona IDを含むsubjectで実行直前に再検査し、一覧時の結果を信頼しない。
  - deny結果を監査logとpersonaへの構造化errorに残し、sub-lineを一切生成しない。
- 必要な回帰:
  - `blocked` / `user_only` / `ask_every_time` / `auto_allow` × user/auto/schedule/run_playbook/execのmatrix test。
  - 一覧にない既知nameを直指定しても実行されないこと。
  - persona AだけcredentialありのPlaybookをpersona Bが呼べず、Aは呼べること。

### [P1] Aspect権限をtext spell/pre-spellだけに掛け、Playbook tool/tool_call/finalizerからrestricted mutatorを実行できる

- 場所: `sea/mode_spell_permissions.py:24-101`, `sea/runtime_llm.py:886-966, 1185-1460, 1784-1940`, `sea/runtime_engine.py:24-140`, `sea/runtime_nodes.py:13-100`, `builtin_data/tools/judgment_finalize.py:70-111`, `builtin_data/tools/meta_judgment_finalize.py:218-250`
- 事実:
  - `check_spell_permission()` を呼ぶのは通常のtext spell loopとpre-spellの2経路だけである。
  - Playbookのstatic `tool` nodeとLLM選択の`tool_call` nodeは、active PulseContext/Aspectを受け取っているのに、名前を直接 `TOOL_REGISTRY` から引いて実行する。
  - finalize系も同じくregistryを直呼びする。現在の生成ロジックは主に許可済み名を選ぶが、中央gateを共有しないため権限表の変更が自動反映されない。
  - `check_spell_permission(..., aspect=None)` はrestricted mutatorも許可するfail-openであり、context伝播漏れがそのまま権限迂回になる。
- 最小再現:
  1. `PulseContext`へ`Aspect.WORKER` frameをpushする。
  2. static tool nodeのactionを`track_complete`にし、registryへ副作用を記録するstubを登録する。
  3. `RuntimeEngine.lg_tool_node(..., auto_mode=True)`を実行すると、WORKERでは禁止されるべき`track_complete` stubが1回実行された。
- 実行確認:
  - 一時回帰を `tests/sea/test_runtime_engine.py` に追加して再現し、確認後に削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-tool-gate tests/sea/test_runtime_engine.py::test_audit_static_tool_node_bypasses_worker_spell_permission`
  - 結果: `1 passed`（WORKER frameでもmutator実行を確認）。
- 影響:
  - WORKER/AUTONOMOUSがTrack lifecycleを変更しないという能力境界は、Playbook定義を経由するだけで回避できる。
  - dynamic tool-callにrestricted名が混入した場合も同じで、表示・spell parserの制限では防げない。
  - addon/user Playbookや将来のfinalizer追加が、中央permission mapを知らずに特権mutatorを呼べる。
- 修正方針:
  - registry callableの手前に単一の`authorize_tool_call(subject, tool_schema, invocation_origin)`を置き、spell/pre-spell/tool/tool_call/finalizer/observer/APIの全入口を通す。
  - mutator権限はtool名の散在setではなくschema capability（track_control/task_control/self_definition等）へ寄せ、aliasにも同じmetadataを引き継ぐ。
  - restricted capabilityでAspect不明はfail closedにし、trusted internal system callだけ署名付きの明示bypass tokenを使う。
- 必要な回帰:
  - 4 Aspect × text spell/pre-spell/static tool/dynamic tool_call/finalizerのmatrixで同じ判定になること。
  - PulseContext欠落、空line stack、nested sub-lineでもrestricted mutatorがfail closedになること。

### [P1] disabled addonのnative toolを起動時に無条件登録し、無効化後もvisible/executableなまま残す

- 場所: `saiverse/data_paths.py:227-269`, `tools/__init__.py:45-99, 245-330`, `sea/head_pipeline/sections/spell_list.py:65-135`, `api/routes/addon.py:378-462`
- 事実:
  - `iter_project_subdirs("tools")` は`expansion_data`配下の全projectを列挙し、AddonConfigの`is_enabled`を見ない。
  - tool autodiscoveryはその全directoryをimport・registry登録する。`addon_name`はmetadataとして付けるだけでenable gateに使わない。
  - SpellListSectionもnative toolについてaddon enabledを検査せず、MCP availability・building・`availability_check`だけで表示する。
  - runtime disable APIが解除するのはMCP、integration、server hook、composite action spellで、addonのnative toolsをregistryから外さない。
- 影響:
  - ユーザーがaddonを無効化しても、そのnative spell/toolはLLMへ提示され、`TOOL_REGISTRY`から実行できる。
  - MCP停止で偶然失敗するwrapperだけでなく、HTTP・filesystem・DB等を直接扱うnative addon toolは無効化後も副作用を起こせる。
  - 「無効化」がUI表示上の状態と実行能力で食い違い、障害隔離・緊急停止として機能しない。
- 修正方針:
  - addon toolをowner単位で登録管理し、startup discoveryでdisabled addonをimport/登録しない。toggle時にnative toolもatomic register/unregisterする。
  - defense in depthとして中央tool authorizationが毎回owner addonのenabled stateを確認し、stale registry entryでも実行を拒否する。
  - alias、provider spec、spell schema、head snapshotをowner単位で同時更新する。
- 必要な回帰:
  - enabled→disable直後（再起動なし）とdisabled状態からのstartupの双方で、native toolがlist/provider spec/registry実行の全てから消えること。
  - unregister途中失敗でもcentral gateが副作用を止め、再enableで重複登録しないこと。

### [P1] Playbook input/set/outputが`_`予約namespaceを書き換え、persona・権限・cancel・Pulse境界を改変できる

- 場所: `sea/playbook_models.py:321-345, 438-475`, `sea/runtime_graph.py:70-205`, `sea/runtime.py:983-1003`
- 事実:
  - Intentは`_` prefixをruntime専用system variableと定義しているが、`InputParam.name`、SetNodeの`assignments` key、output mapping/child outputに予約名validatorがない。
  - initial stateはsystem variablesを並べた後、最後に `**inherited_vars` を展開する。したがってinput schemaに同名を置けば `_persona_obj` / `_pulse_context` / `_spell_enabled` / `_cancellation_token` / `_messages` 等を上書きする。
  - set nodeも任意keyへ `state[key] = value` し、`_` prefixを拒否しない。
- 影響:
  - addon/user Playbookがspell無効設定、active persona、Aspect/line、cancel token、model tier、親message列をruntime途中で改変できる。
  - `_cancellation_token=None`化後の長時間tool、`_spell_enabled=True`化後のLLM node、偽 `_pulse_context`によるdeferred op破壊など、ユーザー停止・人格境界・権限gateをPlaybookデータだけで迂回できる。
  - typoでsystem名と衝突してもload時に失敗せず、挙動だけが静かに変わる。
- 修正方針:
  - PlaybookSchema load時にinput/output/set/output_mapping/argsの全target keyを検査し、`_` prefixを拒否する。
  - stateを`system`と`vars`の物理的に別型へ分け、node APIにはvars viewだけ渡す。runtime nodeだけcapability付きsystem mutation APIを持つ。
  - child outputの親伝播もallowlisted output schema経由に限定し、system namespaceへmergeしない。
- 必要な回帰:
  - 各定義位置で `_messages`, `_persona_obj`, `_pulse_context`, `_spell_enabled`, `_cancellation_token` を指定したPlaybookがload時にvalidation errorになること。
  - 通常args/outputは従来どおり子→親へ渡り、runtime system objectのidentityがPulse中不変であること。

### [P1] realtime spellがspell無効化・Aspect権限・auto-mode判定をすべて迂回して毎Pulse自動実行される

- 場所: `sea/runtime_llm.py:886-966, 1984-2174`, `api/routes/people/realtime_spell.py:16-101`, `api/routes/world.py:779-845`
- 事実:
  - LLM nodeは`state["_spell_enabled"]`を確認する前に、enabled bindingがあれば`_execute_realtime_spells()`を毎Pulse一度呼ぶ。personaの`SPELL_ENABLED=false`はこの経路を止めない。
  - binding作成APIはspell名・引数を検証せず保存し、実行時は`SPELL_TOOL_NAMES`にあるかだけを確認して`_run_spell_tool_async()`へ渡す。
  - `_run_spell_tool_async()`は`check_spell_permission()`を呼ばず、active Aspectにかかわらずregistry callableを実行する。さらに`persona_context(auto_mode=False)`を固定するため、実際はschedule/auto Pulseでも下流のauto-mode判定へuser起動として伝わる。
  - building bindingとpersona bindingは同じ実行器へ入り、building側の設定者がrestricted mutatorを指定した場合も対象personaのAspect権限を再確認しない。
- 影響:
  - spellを無効化したpersonaにも外部I/O・状態変更spellが自動実行される。
  - WORKER/AUTONOMOUSでは禁止されるTrack・自己定義系mutatorをbinding経由で反復実行できる。
- 修正方針:
  - realtimeを含む全入口を中央`authorize_tool_call`へ通し、`SPELL_ENABLED`、Aspect、Pulse種別、owner building、addon enabledを実行直前に評価する。
  - binding作成時にもschema・引数・owner存在を検証するが、保存時検証だけを実行時gateの代用にはしない。
  - 自動実行であることをcontextへ正しく渡し、user-only/ask-every-time capabilityを暗黙許可しない。
- 必要な回帰:
  - `SPELL_ENABLED=false`、4 Aspect、user/auto/schedule、persona/building bindingのmatrix。
  - binding保存後にpermission/addon/building所属が変わった場合も実行時に拒否されること。

### [P2] Playbookの入力contractを実行時に検証せず、required欠落・型違い・enum外値を暗黙値として実行する

- 場所: `sea/playbook_models.py:438-468`, `sea/runtime_graph.py:67-84`
- 事実:
  - `InputParam`は`param_type`・`required`・`enum_values`を持つが、runtimeは`_args`から値をコピーするだけで型・enumを検証しない。
  - required引数が無い場合もerrorにせず、defaultが無ければ空文字を代入する。未宣言argsは黙って捨てる。
- 影響:
  - function-call/UIが契約違反を成功扱いにし、空の対象IDや文字列化された数値で別処理へ進む。入力不正とPlaybook内部失敗を監査上区別できない。
- 修正方針:
  - Playbook起動の単一境界でrequired/default/type/enum/unknown-keyを検証・正規化し、node実行前に構造化errorを返す。
- 必要な回帰:
  - required欠落、number/boolean coercion、enum外値、unknown key、default適用のcontract test。

### [P2] `run_playbook`のnot-found応答がscope・permission・credentialを無視して非公開Playbook名を列挙する

- 場所: `builtin_data/tools/run_playbook.py:206-235`
- 事実:
  - 指定名が見つからない場合、runtime cacheまたはDBの`router_callable`全件を列挙する。
  - persona/city scope、developer-only、city permission、required credentialを適用しない。
- 影響:
  - 実行権限のないaddon・user・developer Playbook名がpersonaのLLM contextへ戻り、存在と命名から機能・導入状況を漏らす。
- 修正方針:
  - 認可済み一覧を生成した同一snapshotだけを候補表示に使う。認可前のnameはnot-foundとpermission-deniedを外向けに区別しない。
- 必要な回帰:
  - persona/city/credential/dev状態を跨いで、応答に許可済み候補だけが現れること。

## Coverage

- Spell: text loop、pre-spell、realtime binding、static tool、dynamic tool_call、finalizer、nested `run_playbook`。
- Tool registry: builtin/user/addon native、MCP、building gate、availability、provider spec、alias/composite action、disabled addon toggle。
- Context: persona/building/manager/playbook/auto-mode/PulseContext/LLM messagesのContextVar設定・resetとthread executor伝播。
- Playbook: list/load/run/exec/subplay、scope/dev/city permission/credential/router-callable、input/set/output state namespace。
- 未確認: 各外部provider固有toolのremote-side authorizationは「外部連携」行で監査する。

## 集計

- **P1×5 / P2×2**
- 一次監査は完了。以後は修正追跡と回帰固定へ移る。

## 修正追跡（2026-07-16・第一陣）

- **修正・回帰固定 [P2×1]**: `run_playbook`のnot-found候補をruntime cache/DB全件列挙から、`list_available_playbooks()`の同一認可gate（scope、developer mode、credential、city permission、auto mode）へ一本化した。外向け応答は許可済み候補だけを返し、候補生成自体が失敗した場合は名前を一件も列挙しない。

## 修正追跡（2026-07-16・第二陣 — 共通境界 hardening による消し込み。Fable検証済み）

第二陣（`docs/intent/audit_second_batch_hardening.md`）の「実行能力の入口は一つ」不変条件の実装が、本監査のP1×3を消し込み、P1×1を部分修正した。

- **修正・回帰固定 [P1] Playbook一覧のcity権限を`run_playbook`実行時に再検査しない**: `run_playbook()` が起動直前に required_credentials と city `PlaybookPermission`（blocked / user_only / ask_every_time）を再検査するようになった。`ask_every_time` はユーザー対話Pulseの明示承認経由でだけ実行される。回帰: `tests/test_run_playbook_spell.py::test_run_playbook_rechecks_denied_city_permission` / `::test_run_playbook_ask_every_time_requires_interactive_conversation` / `::test_run_playbook_ask_every_time_executes_after_allow`。
- **修正・回帰固定 [P1] Aspect権限をtext spell/pre-spellだけに掛けている**: `TOOL_REGISTRY` への登録時に全callable（sync/async）を `tools/__init__.py::_wrap_with_authorization_gate` でラップし、実行直前に `check_spell_permission()`（Aspect権限）・addon有効状態・`availability_check` を再検査する。static tool node / dynamic tool_call / finalizer / realtime / composite action は registry callable 経由なので同一ゲートを通る。composite action はゲートをunwrapしないことも固定。回帰: `tests/test_tool_execute_authorization.py`。**残課題**: `check_spell_permission(aspect=None)` の fail-open は維持されており、PulseContext 伝播漏れ時のrestricted mutatorは通る（findingの修正方針後段は未対応）。
- **修正・回帰固定 [P1] disabled addonのnative toolが実行可能なまま残る**: 上記共通ゲートが実行直前にowner addonの有効状態を確認し、stale登録でも実行を拒否する。回帰: `tests/test_tool_execute_authorization.py::test_disabled_addon_native_tool_is_denied_even_if_still_registered`。**残課題**: 「startup discoveryでdisabled addonを登録しない／toggle時のatomic unregister」は未実装で、拒否層（defense in depthの片翼）のみ。spell一覧表示からの除外も未確認。
- **部分修正 [P1] realtime spellの権限迂回**: 個々のspellが registry callable 経由になったため、Aspect権限とaddon有効状態は実行直前に検査される。**未修正**: persona の `SPELL_ENABLED=false` はrealtime実行を止めない（`_execute_realtime_spells` は `_spell_enabled` gateの外で毎Pulse実行）。`persona_context(auto_mode=False)` 固定も残存。
- **未修正 [P1×1 / P2×1]**: `_`予約namespaceのPlaybook上書き、Playbook入力contractの実行時検証。
