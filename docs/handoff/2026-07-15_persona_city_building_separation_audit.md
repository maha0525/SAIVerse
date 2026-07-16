# Persona / City / Building 分離 一次監査

**開始・完了日**: 2026-07-15  
**状態**: 指摘あり・一次監査完了  
**監査基準**: `113567e`  
**監査軸**: personaの単一占有、DB/in-memory/イベントの原子性、City間移送、RemotePersonaProxy、Building/Region参照整合性、ユーザーの発言契機入室

## 現行Intentの再確認

- `docs/intent/building_memory_unified.md`
- `docs/intent/region.md`
- `docs/intent/version_aware_world_and_persona.md`

正典上、ユーザー位置は`User.CURRENT_BUILDINGID`の一箇所、AI位置はactive occupancy一件であり、移動とoccupancy eventは一つの意図的移動に対応する。Region入口は必ず親scopeに属し、City間来訪は少なくとも同一versionでなければならない。

## Findings

### [P1] 移動DBを先にcommitし、occupants・イベント・persona現在地を別々に更新するため、失敗結果と実世界が分裂する

- 場所: `saiverse/occupancy_manager.py:167-379`, `manager/runtime.py:169-231, 286-362`, `saiverse/day_plan.py:2125-2184`
- 事実:
  - `move_entity()`はactive occupancyのclose/new rowまたは`User.CURRENT_BUILDINGID`を更新してlocal sessionをcommitした後、in-memory occupants、leave/enterの2イベント、dynamic state、addon hook、game lifecycleを順に実行する。
  - commit後の`add_building_event()`等が例外になるとouter `except`はrollbackして`False`を返すが、既にcommitしたDBは戻らない。occupantsも途中までmutation済みになり得る。
  - callerは成功時だけ`persona.current_building_id`やuser state cacheを更新する。したがって「失敗」時にDBだけ移動済み、occupantsは移動済み、persona属性は旧Buildingという組合せが成立する。
  - 外部`db_session`を渡す経路は逆に、commit前にin-memory/eventを公開する。callerが後でrollbackするとDBだけ旧位置へ戻る。
- 影響:
  - 同じpersonaのhead、audience、schedule、tool building gate、occupancy eventが別々のBuildingを参照する。再起動前後で現在地が変わり、失敗したはずの移動が復活する。
- 修正方針:
  - DBのlocation transitionとoutbox eventを一transactionで確定し、commit成功後に一つのcanonical snapshotからin-memoryを更新する。公開後処理失敗はmove自体を失敗に巻き戻すのではなく、outbox再配送状態として記録する。
  - persona/user属性の更新責務をcaller群から移動serviceへ集約する。
- 必要な回帰:
  - old event成功→new event失敗、hook失敗、external transaction rollback、process restartの各fault injectionで、DB・occupants・persona属性・eventsが同一transitionを示すこと。

### [P1] active occupancyの一意制約と現在地CASがなく、同一personaを複数Buildingへ同時配置できる

- 場所: `database/models.py:249-264`, `manager/persona.py:241-298`, `saiverse/occupancy_manager.py:167-246`
- 事実:
  - `building_occupancy_log`に「AIIDごとに`EXIT_TIMESTAMP IS NULL`は最大一件」のDB制約がない。
  - startup loaderはactive rowを全件読み、同じpersonaを複数の`occupants[building]`へappendする。`persona.current_building_id`はDBの無指定順で最後に読んだrowになる。
  - `move_entity()`はcallerの`from_id`がcanonical current locationか検証せず、そのBuildingのactive rowだけcloseする。stale `from_id`では旧active rowを残して新active rowを追加する。
  - in-memoryの旧Buildingにentityがいなくても移動を続行する。
- 影響:
  - personaが複数室の発言を聞き、複数の自律/schedule対象となる。capacity、building tool権限、private contextが同時に複数scopeへ開く。
- 修正方針:
  - partial unique index等でactive rowをAIID単位に一意化し、移動をcanonical active rowに対するCAS updateとして実装する。startupは重複を警告するだけで任意選択せず、隔離・修復対象にする。
- 必要な回帰:
  - 同時移動、stale from、既存duplicate row、再起動の全てで二重presenceが成立しないこと。

### [P1] `/chat/send`が非現在地Buildingへの発言を許し、`/chat/utter`も移動と発言をtransaction化していない

- 場所: `api/routes/chat.py:908-1027, 1030-1110`, `manager/runtime.py:476-620`
- 事実:
  - `/chat/send`はrequestの`building_id`をそのまま採用し、`User.CURRENT_BUILDINGID`との一致もoccupants所属も検査せず、そのBuildingのpersonaへuser発言を配送する。
  - `heard_by`には現在地確認なしでuser IDを加えるため、ユーザーが不在のBuildingで「その場にいた」履歴が生成される。
  - `/chat/utter`のdocstringは移動と発言を一transactionとするが、実装は`move_user()`をcommitした後に`send_message()`を呼ぶだけである。attachment処理、stream開始、message insert、LLM起動の失敗時に移動を戻さない。
  - `client_message_id`はbuilding messageだけの冪等keyで、移動transitionを含まない。
- 影響:
  - 単一位置モデルをAPI一呼び出しで迂回できる。発言が残らないのに入室だけ成立し、retryはCAS conflictになり得る。
- 修正方針:
  - raw `/send`はserver current Building専用にし、別Building指定を拒否する。utterはlocation CAS・occupancy outbox・user message idempotencyを一つのcommand/idempotency keyで確定する。
- 必要な回帰:
  - raw sendの別室拒否、move後message insert失敗、同一utter retry、並行deviceのmatrix。

### [P1] City dispatchの確定処理が実行されず、residentとremote proxyが同時に別Cityへ存在する

- 場所: `manager/visitors.py:15-120, 192-350`, `manager/background.py:113-140`, `database/models.py:275-285`
- 事実:
  - destinationがrequestをacceptedにするとsourceの`_check_dispatch_status()`はmodelに存在しない`target_city_name`と`current_building_id`属性を読むため例外になる。
  - residentのactive occupancyを閉じて`IS_DISPATCHED=True`にする`_finalize_dispatch()`は定義されているが呼び出しが存在しない。
  - destinationは同じpersona IDの`RemotePersonaProxy`をoccupantsへ追加済みなので、source residentも元Buildingに残ったまま二City同時presenceになる。
  - accepted visitor proxy/occupancyはmemoryだけで、destination再起動時に再構築されない。accepted rowは処理対象外であり、再dispatch時は既存rowとして永久に拒否される。
- 影響:
  - persona identity、発話者、記憶の帰属、capacityがCity間で分裂する。再起動と通信再試行で所在を決定できない。
- 修正方針:
  - dispatch IDを持つ明示state machineと、source prepare→destination accept→source commit→destination activateの冪等handshakeを実装する。各stateに必要なfieldをschema化し、proxy presenceを永続化・startup再構築する。
- 必要な回帰:
  - accept前後の両City crash、retry、reject、return、destination restartでpersonaが常に一箇所だけactiveであること。

### [P1] 来訪profileを本人・source City・versionと結び付けず、dispatched resident IDを名乗る入力を「帰宅」として適用する

- 場所: `database/api_server.py:24-86`, `manager/visitors.py:15-50, 192-256`, `docs/intent/version_aware_world_and_persona.md`
- 事実:
  - `/inter-city/request-move-in`は認証・署名なしでpersona ID/name/emotion/source/targetを受理する。profileにSAIVerse/persona versionは無く、暫定仕様のversion一致拒否を実装できない。
  - destination側は`pid`がlocal residentで`is_dispatched=True`なら、sourceの一致やdispatch transactionを検証せず「returning persona」と扱い、location・emotion・occupancyを更新する。
  - source city名もfree-form payloadであり、directory上のsenderと結び付かない。
- 影響:
  - 外部入力がpersona IDを名乗るだけで帰還・移動・emotion更新を起こせる。異version状態も同じpersonaとして混在する。
- 修正方針:
  - City identityで署名されたone-time dispatch token、source/destination/persona/version/targetのbinding、replay防止を必須にする。帰還は未完了dispatch transactionとの一致時だけ許可する。
- 必要な回帰:
  - forged source、wrong persona/target/version、expired/replayed token、正規returnのcontract test。

### [P1] Region更新・Building再所属で「入口は親scope」の不変条件を破壊できる

- 場所: `manager/admin.py:510-748`, `docs/intent/region.md:33-83`
- 事実:
  - `update_region()`は`PARENT_REGION_ID`を変更するが、`ENTRANCE_BUILDING_ID`が指すBuildingの`REGION_ID`を新しい親scopeへ同期しない。
  - `set_building_region()`は対象BuildingがどのRegion/SubRegionの入口か確認せず、入口を任意Region（自分自身を含む）へ再所属またはdetachできる。
  - top→sub変更では入口がCity直下に残り、入口から内部への移動はnew scopeが2段になるため`_check_entrance_topology()`に拒否される。入口を自Region内部へ入れると外側から入口自体が見えなくなる。
- 影響:
  - Region/SubRegionが通常移動では到達不能になるか、policy執行点とmap scopeが食い違う。
- 修正方針:
  - parent変更とentrance所属変更を同transactionで行う。入口Buildingの直接再所属を拒否し、Region serviceだけが不変条件付きで変更する。
- 必要な回帰:
  - top↔sub変換、入口detach/self/別Region割当、移動topologyとmap表示の整合。

### [P1] BuildingのCity変更がuser位置・Region・private room等の参照scopeを検査せず、City境界を跨いだ参照を作る

- 場所: `manager/admin.py:444-507`, `database/models.py:45-166`
- 事実:
  - `update_building()`がCity変更を拒否するのはactive AI occupancyがある場合だけである。
  - `User.CURRENT_BUILDINGID`、Buildingの`REGION_ID`、AIの`PRIVATE_ROOM_ID`、tool/item/link等を検査・移送しない。target Cityの存在確認もこのmethod内にない。
  - userが現在いるBuildingはCityだけ変更でき、`User.CURRENT_CITYID`は旧Cityのままになる。Region所属BuildingもRegion.CITYIDと異なるCityへ移せる。
- 影響:
  - startupのuser occupancy復元が消え、Region map/gate、private room、Building-scoped toolが異Cityのentityを参照する。
- 修正方針:
  - City変更を専用migration commandへ分離し、全参照を検査・一括更新する。通常updateではCITYIDをimmutableにする。
- 必要な回帰:
  - user/AI/Region/private room/item/tool linkを持つBuildingのCity変更を拒否し、通常field更新に影響しないこと。

### [P2] occupancy event keyが秒単位で、同一秒の同経路移動を同一eventとして衝突させる

- 場所: `saiverse/occupancy_manager.py:276-305`
- 事実:
  - `event_key`は`entity_id/from/to/int(now.timestamp())`で構成される。同一personaが同一秒にA→B→A→Bと動けば同じA→B keyが再利用される。
  - recall/ingestの冪等判定はevent keyを識別子として扱うため、別の意図的移動を重複と誤認し得る。
- 修正方針:
  - DBで採番したmovement/transition UUIDをleave/enter共通keyにし、再試行時だけ同じkeyを再利用する。

### [P2] startupの不正occupancyはwarningだけで稼働を継続し、canonical locationを監査・修復できない

- 場所: `manager/persona.py:241-298`
- 事実:
  - missing persona/Building rowはstartup warningにするが、duplicate active row、capacity超過、dispatched residentのactive row、Building.CITYID不一致を分類しない。
  - 起動後にどのrowをcanonicalとしたかの修復記録もない。
- 修正方針:
  - startup consistency checkerで異常種別を列挙し、人格境界に関わる重複・cross-cityは該当personaを停止/隔離する。修復は明示transactionとaudit logで行う。

## Coverage

- resident persona/userのstartup load、move/summon/end conversation/editor/day-plan経路。
- active occupancy DB、in-memory occupants、persona/user current location、leave/enter building event。
- Region/SubRegion入口、所属変更、City変更、capacity・private room参照。
- legacy inter-city DB polling、HTTP move-in、dispatch/return、RemotePersonaProxy lifecycle。
- 未確認: Discord gateway独自visitor/memory transferの暗号・外部入力境界は「外部連携」行で監査する。

## 集計

- **P1×7 / P2×2**
- 一次監査は完了。以後は修正追跡と回帰固定へ移る。
