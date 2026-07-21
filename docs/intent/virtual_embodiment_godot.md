# Intent: Godot / ARDY 仮想身体デモ

> **ステータス: v0.17 (2026-07-20) — 実装中**  
> 2026-07-12 の「VR/3Dアバターの身体制御」構想を、Godot + OpenXR + VRM + ARDY による公開デモ計画へ昇格した。Unity は正典から外し、既存 `unity_gateway` は再利用可能な知見を回収するための旧実装として扱う。

## 実装記録

### 2026-07-19: `player.vrm`材質白化の根因と誤った補正の撤去

- `player.vrm`はGodotへのコピー元`C:\Users\shuhe\workspace\senseivrm\sensei_FBXV1.01.vrm`とSHA-256が一致した。VRM内には全4材質のbase textureがある一方、`shadeColorFactor=[1,1,1]`、`shadeMultiplyTexture`なしとして記録されていた。同じUniVRM 0.131.1で生成した`persona.vrm`にはshade textureが入っているため、コピー破損、Godot importer、UniVRM全体の不具合ではない。
- Unity元材質は現在MToon10 URP shaderを参照する一方、`_utsVersion=2.09`、`_Use_BaseAs1st=1`、`_1st_ShadeColor`、`_NormalMap`、`_Emissive_Tex`、`_MatCap_Sampler`などUnity Toon Shader固有の値を保持していた。MToon側の`_ShadeTex`、`_BumpMap`、`_EmissionMap`、`_MatcapTex`は未設定だった。根因はUTSからMToonへshader参照だけを切り替え、材質parameterを対応項目へ移植しなかったこと。白化は影設定の欠落が表面化した一症状で、normal・emission・matcapもVRMへ出力されていない。
- Vessel側で「base textureあり・shade textureなし・shade color白」の材質へbase textureを代入する処理を一度実装したが、これは出力を見た目だけ合わせ、編集可能な上流アセットの変換不備を隠す誤った対処だった。`repair_missing_mtoon_shade_texture`、scene opt-in、専用テスト期待値、README記述を撤去し、VRM材質を記録どおり描画する状態へ戻した。
- Unity側の材質移植と再exportはアバター制作工程で行う。Vesselは今後も、モデル作者の明示した材質を推測で上書きしない。今回の失敗と、最初の再発防止策まで局所最適に陥った経緯は[`session_reflection_2026-07-19_asset_workaround.md`](../session_reflection_2026-07-19_asset_workaround.md)へ記録し、一般規範は`CLAUDE.md`の「Whole-system correction and recurrence prevention」へ正典化した。

### 2026-07-20: 再export済み`player.vrm`の境界横断検証

- 差し替え後の`player.vrm`は42,631,356 bytes、SHA-256 `927d23890f37520d83cdd2a0031ed8b38796b26d0a386b0db9dcaf3ca54e2ed8`。GLB宣言長と実長・chunk終端が一致し、外部URI参照のない自己完結したUniVRM 0.131.1 / VRM 1.0だった。Humanoidは51 mappingで必須15骨と左右Eyeが揃い、眼中央がHeadより`+Z`へ0.023621mあるため、Vesselの正面判定でもVRM 1.0の`+Z`正面として扱える。
- 5材質すべてにbase texture、非白の`shadeColorFactor`、`shadeMultiplyTexture`が格納された。服・髪にはnormal、emission、matcapも戻り、前回の4材質すべてでshade/normal/emission/matcapが欠落していたartifactとは内容が明確に異なる。全mesh primitiveのmaterial参照は有効で、埋め込みimage 13件にも外部依存はない。
- Godot 4.6.3とVessel同梱のVRM/MToon addonだけを置いた隔離projectで、新旧VRMを同一import parameterにより比較した。新VRMは1 `Skeleton3D`・最大165骨・18 `MeshInstance3D`・5 MToon材質としてimportされ、runtime materialにはmain 5、shade 5、normal 2、emission 4、matcap 2 textureが残った。旧VRMはmain 4に対してshade/normal/emission/matcapがすべて0だった。
- RTX 3090 / Vulkan / Forward+で両者を同じcamera・照明条件により実描画した。旧VRMは白く飽和した一方、新VRMは衣服・髪・身体の陰影と材質差が描画され、白化の原因だったexport欠落が解消した。これはVessel側の材質補正を一切使わない検証である。
- 新VRMの`VRMC_vrm.expressions`はpreset 0・custom 0、animation 0だった。現在のplayer body表示・移動には支障がないが、将来プレイヤーの表情・口形・face trackingをペルソナ視覚へ渡す場合、このVRMだけでは表情を駆動できない。必要になった時点で制作元に表情定義を持たせるか、player用のモデル固有profileを別途所有する。
- 実プロジェクトの既存`.godot/imported/player...scn`は検査時点で旧VRMの生成時刻のままであり、開いていたeditorはまだ新artifactへ再importしていなかった。データ自体のimport・runtime material・描画は隔離projectで確認済みだが、Vessel本体のplayer slotとペルソナ一人称撮像を通す最終integration証拠は、本体側の再import後に別途取得する。

### 2026-07-20: ユーザーアバターの基礎idle

- 最終成果は「VRMにanimation fieldがあること」ではなく、ペルソナの視界でユーザーがTポーズの物体ではなく、同じ空間に自然に立つ身体として見えること。idleの所有者はVRM exporterではなくVesselのplayer-body runtimeとし、ユーザーが差し替えた任意Humanoid VRMへ同じARDY資産をretargetする。
- 身体形状やVRM metadataから性別を推測して動きを自動選択しない。初期資産として中立、男性的な例、女性的な例に相当する3種類をARDYで用意するが、runtime IDは`neutral`、`grounded`、`soft`のstyle特性で表し、ユーザーがどのアバターにも自由に選べる。プロンプト由来・生成条件・loop seamをasset metadataとcatalogへ残す。
- player idle catalogをデータとして所有し、`default_style`と各styleのasset path・表示名・説明を定義する。`PlayerController`は`PlayerAvatar`の既存`AvatarLoader` APIだけを通して選択assetをloadし、loopとcrossfadeを開始する。未知style、catalog破損、asset欠落、loop品質不良は構造化ログへ残し、Tポーズへ黙って戻ったように見せない。
- 歩行clipが未実装の段階で移動入力時にidleを停止すると、移動中だけTポーズへ戻るため、初期実装ではidleを基礎姿勢として常時維持する。次にplayer locomotionを追加した時点で、入力状態を所有する`PlayerController`が`idle ↔ locomotion`を切り替える。HMDの頭・手trackingを重ねる段階では、trackingを上半身の権威とし、idleは骨maskまたはIK後段により呼吸・骨盤・下半身の微動へ縮退させる。
- 検証はcatalog解決だけで終えず、実`player.vrm`へのretarget、Tポーズからの腕角度変化、root水平移動が実質0であること、loop seam、style切替crossfade、ペルソナcameraからの実描画まで追う。
- 上記を実装した。ARDY Core8へ英語で指示し、4秒・20 FPS・10 diffusion stepからloopを切り出した`grounded`、`soft`、`neutral`の3 assetとcatalogを追加した。採用clipはいずれも31 frameで、loop seam最大関節差は順に7.192°、5.095°、0.671°、root Y差は最大1.290 mmだった。生成prompt、seed、元frame範囲、loop品質は各asset metadataへ残した。
- 最初の実描画では、ARDY元データ上の両手は腰付近にあるのに`player.vrm`だけ両腕が前へ伸びた。promptやclipを作り直す問題ではなく、`player.vrm`のSkeleton3DローカルRest位置がZ-upである一方、従来bridgeがY-up固定だったことが根因だった。`persona.vrm`はY-upで、同じclipが正常だったため、VRMごとの局所補正ではなくretarget境界の座標契約を修正した。
- bridgeはHips→Headを身体の上、右上腕→左上腕を身体の左として3軸の解剖学的basisをRest位置から復元し、ARDYのglobal rotationとroot translationの双方を共役変換する。人体の湾曲を軸傾斜と誤認しないよう、主軸との内積が0.9以上なら符号付きX/Y/Zへsnapする。結果はplayerが`X→+X, Y→+Z, Z→-Y`、personaが厳密な恒等変換になった。
- Godot VRM importerの`Skeleton3D.motion_scale`はHipsのローカルY成分から算出されるため、Z-upなplayerでは実身長に反して0.0292 mだった。bridgeはimport値と腰―足間の解剖学的高さを比較し、不整合時だけ腰・足からscaleと接地基準を再計算する。playerは0.9436 mと足高0.0645 m、personaは互換な既存値1.0 mを維持した。これにより現在のIdleだけでなく、次のplayer歩行でも同じ軸・scale契約を使える。
- 実`player.vrm`の数値検証では3 styleすべての両手が肩より0.354〜0.461 m下、水平root driftは1 mm未満、groundedの腰高誤差は0.033 m、0.3秒crossfade中点は0.5だった。実`persona.vrm`のwaveは座標basisが恒等、右手移動0.544 m、右腕最大変化104.02°の既存値を維持し、synthetic walkのblend-out回帰も通過した。Forward+実録画でも3 styleすべてがTポーズを離れ、自然な立位として描画された。検証はすべてofflineで、Gateway、LLM、本番ペルソナ、本番記憶には接触していない。

### 2026-07-19: デスクトップ解像度とペルソナ一人称視覚の是正

- デスクトップ表示は性能上の制約ではなく未設定のGodot初期サイズだった。初期表示を1920×1080、リサイズ可能とし、XRの推奨render targetとは分離する。
- `body_see`の画像はGeminiのタイル課金を前提に1024×1024へ変更する。提示された計算では正方形を2×2の4タイルに収め、横長画像で同じ細部量を得るより短辺を有効に使う。連続撮像へ進む際は、解像度だけでなく撮像頻度・差分判定・送信条件を別途設計する。
- 一人称カメラを固定高に置かず、VRM Humanoidの左右Eye（片眼しかない場合はその眼、眼がなければHead）から基準transformを構成し、現在のHead poseへ追従させる。解決根拠、眼高、forward offsetを構造化ログへ残す。
- ペルソナ本体を専用render layerへ置き、一人称カメラからそのlayerを除外する。これは頭部だけでなく自己アバター全体を一人称画像から外す安全側の規則で、将来の客観視カメラは別に全身を描画する。プレイヤー用XRCameraがプレイヤー自身のlayerを除外する既存規則と対称にする。
- プレイヤーVRMが白い代替表示になる問題は、元VRMにbase-color textureがあることを確認したうえで、実SubViewportのmaterial・texture・shader状態と撮像結果から根因を特定する。未診断のまま材質を差し替えない。
- 上記を実装した。`project.godot`のdesktop viewportを1920×1080・リサイズ可能にし、`body_see`のSubViewportを1024×1024へ変更した。現在の`persona.vrm`では左右Eye中央の高さ1.389073m、model-forward offset 0.04mへカメラを配置し、Head poseとAvatarAnchorの移動・回転へ追従する。
- ペルソナ全VisualInstanceをlayer 4へ置き、一人称camera maskからlayer 4を除外した。通常のXRCameraとExpression Studioはlayer 4を描画するため、第三者視点の全身表示と表情編集は維持する。実撮像で画像下端にあった自己頭部が消えた。
- 1024角PNGは実測139,548 bytesだったが、base64を含むWebSocket eventがGodot既定outbound bufferを超えて最初のE2Eで`ERR_OUT_OF_MEMORY`になった。outboundを8 MiB、inboundを1 MiBへ明示し、送信失敗時に`lifecycle_sent`を記録しないよう修正した。再E2Eは合成`e2e_persona`、一時`SAIVERSE_HOME`、AMD 890M、外部LLM 0回で1024×1024 / maximum contrast 255 / media source `godot_persona_first_person`を確認してexit 0だった。この撮像値は後に撤去した材質補正が有効な時点の記録であり、現行`player.vrm`の材質表示を保証する証拠には使わない。撮像transportとbuffer修正の証拠としてのみ扱い、材質の実描画はUnity側の再export後に再検証する。

### 2026-07-19: ユーザー制作の表情プリセット

- 表情の作者と利用者を分ける。アバター所有者はGodot上で実際の顔を見ながらBlend Shapeを調整し、モデル固有の表情プリセットを作る。ペルソナは完成済みプリセットの名前・説明・強度をSpellで選ぶが、視覚フィードバックなしに頂点変形の配合を作らない。
- `Expression Studio`は現在のペルソナVRMからMeshInstance3DとBlend Shapeを列挙し、検索、単独スライダー調整、複数targetの合成、名前・英語説明・channel・遷移時間の編集、プレビュー、保存を行う。編集UIはデスクトップで明示的に開いた時だけ入力を捕捉し、通常のVessel動作やXRを止めない。
- profileは`saiverse.avatar_expressions.v2`とし、VRMファイルのSHA-256、プリセット説明、channel、fade-in/out、targetを持つ。target解決は旧NodePathの完全一致を第一候補に残すが、再書き出し耐性の正典は`blend_shape + mesh_name`、それでも一意なら`blend_shape`単独とする。候補が複数なら推測せずunresolvedとし、Studioでユーザーが選び直す。
- `resolved_target_count=0`のprofileやtargetなしの非neutral表情は成功扱いしない。モデルhash不一致、欠損、曖昧候補、解決方式は構造化ログへ残す。旧v1 profileは読めるが、Studio保存時に現行モデルhashと安定targetへ昇格する。
- 感情、瞬き、口形、視線を独立channelのまま維持し、表情遷移はchannel内の現在値から次のrecipeへBlend Shape値を補間する。gesture開始前の感情状態を保存し、開始時に指定presetへfade、自然完了・失敗・`body_stop`の全経路で元の状態へfade-backする。
- `body_gesture`へ任意の`expression_preset`と`expression_intensity`を追加する。ARDYの英語`action_instruction`は身体だけを生成し、顔は別引数で明示する。未指定時は現在表情を維持し、未知presetを黙ってneutralや笑顔へ読み替えない。生成待ちgestureでも同じ表情指定をpending commandへ保持する。
- 自動瞬き、ユーザー追従視線、発話visemeは同じ表情基盤を後から使う常時layerだが、ユーザー制作の感情presetと混ぜて実装しない。最初の通過条件は「Studioで作ったpresetを保存・再読込でき、gesture中だけ適用され、終了後に元の顔へ戻る」こと。
- 上記を実装した。差し替え後の`persona.vrm`には740個、`player.vrm`には291個のBlend Shapeが残っていた。旧profileのmesh pathは外れていたが、23 target全件を`blend_shape`の一意解決で復旧し、17表情を再び利用可能にした。旧モデルhashとの不一致は隠さず、Studioで次に保存した時点で現VRM hashとv2 targetへ昇格する。
- `F4`で開くExpression Studioをメインシーンへ追加した。検索可能な740項目、複数target配合、ID・説明・channel・fade-in/out、編集前の顔へのreset、保存後の再configure、保存済み遷移プレビューを実装した。顔専用の近接プレビューと左編集欄のスクロールを備え、AMD 890M / Forward+ / 1152×648の実描画で全項目へ到達できることをスクリーンショット確認した。
- 実VRMテストでは`happy=0.8`の遷移途中でBlend Shape最大差0.30、終了時の元顔との差0.0を確認した。v2 profileの隔離保存はschema、VRM SHA-256、再読込後のtarget解決が一致した。未知presetは開始せず、`body_stop`でも元顔へ復帰した。
- 合成`e2e_persona`、一時`SAIVERSE_HOME`、一時Godot user-data、実WebSocketで`body_gesture(intent="friendly_wave", expression_preset="happy", expression_intensity=0.75)`を完走した。0.2秒fade-in、2.95秒の実ARDY wave、0.3秒fade-back、`completed`を同一command IDで確認した。アドオンPython単体39件、全Python `ruff check`、Godot全GDScript parse、方位・歩行・プレイヤー操作・実ARDY回帰がexit 0だった。
- 新規を含むmain-scene回帰fixtureは`SAIVERSE_EMBODIMENT_WS_URL=ws://127.0.0.1:1`をプロセス内で強制し、意図せず起動中のSAIVerse Gatewayへ接続しない。表示確認中に既定`localhost:8000`へ認証前の接続試行が一度発生したが、Body command、ペルソナ、LLM、記憶、世界状態への操作は無かった。以後のfixtureは到達不能URLへ固定した。

### 2026-07-19: VRM 0.x / 1.0 の正面規約をVessel内で統一

- VRM公式仕様とGodot 4.6公式規約を照合し、VRM 0.xは`-Z`、VRM 1.0とGodotの3D assetは`+Z`をモデル正面、GodotのCamera3Dは`-Z`を前方とする境界を確定した。
- 差し替え後の`persona.vrm`と`player.vrm`はいずれもUniVRM 0.131.1によるVRM 1.0で、Humanoid眼球のRest位置が頭部の`+Z`側にある正規の書き出しだった。アバター側を誤りとして再変換せず、Vessel側の旧`-Z`前提を修正した。
- `avatar_loader.gd`は読み込み後のHumanoid頭部・眼球Rest位置から実際のモデル正面を判定する。`+Z`は無補正、VRM 0.x等の`-Z`はinstanceだけをY軸180度回し、親のVessel契約を常に`+Z model front`へ正規化する。判定根拠・source front・補正角は構造化ログへ残す。
- `AvatarAnchor`の固定180度回転を外し、`body_move_to`は`look_at(..., use_model_front=true)`で`+Z`をユーザーへ向ける。`PersonaView`はCamera3D固有の`-Z`前方を身体の`+Z`正面へ一致させるためローカルY軸180度、プレイヤーVRMはXRCameraの`-Z`へ身体正面を合わせるため`PlayerAvatar`をローカルY軸180度とした。
- 新VRMはSkeleton3Dを2本持ち、主要Humanoid bone名も`UpperArm_R` / `LowerArm_R`形式だった。Motion Playerを「先頭Skeleton固定」からHumanoid alias一致数による選択へ変え、Godot canonical名とUniVRM由来名の双方をCore27へ対応させた。
- 合成Skeleton方位テストで`+Z`無補正、`-Z`180度補正、model-front注視、PersonaView前方一致を確認した。実VRM統合テストではペルソナ対面・PersonaView・プレイヤー身体のalignmentがすべて`1.0`、ARDY 23骨、右腕最大変化約`104.02°`、右手移動約`0.544m`、歩行blend-out後Idle復帰をexit 0で確認した。
- 差し替え後のVRMでは、旧`persona.expressions.json`のmesh pathが一致せず、17表情のresolved targetが0件であることもログから判明した。これは正面規約とは別のモデル固有profile適合として扱い、今回の修正で推測マッピングしない。

### 2026-07-17: Godot / Codex 制御基盤

- Godot `4.6.3-stable` の実体とCLI起動を確認した。
- 最小プロジェクトを `Forward+` / Vulkan / Jolt Physics で初期化した。
- `hi-godot/godot-ai` `v3.0.2` をプロジェクト内アドオンとして固定導入した。
- SAIVerse の既存サービスが `8000` を使用していたため、Godot AI を HTTP `8010` / WebSocket `9510` に固定した。
- Codex のリポジトリスコープMCP設定を追加し、MCP初期化、43ツールの列挙、実エディタに対する `editor_state` 成功まで確認した。
- Godot AI は `user` モード、テレメトリ無効で固定した。
- Godot XR Tools `4.5.1`、Godot VRM `2.5.7`、同梱 MToon `3.4.0` を固定導入した。
- OpenXR action map、HMDカメラ、左右コントローラー、低ポリゴン手、90Hz開始ノードを持つ基礎シーンを作成した。
- `assets/avatars` の最初の `.vrm` を自動配置し、Skeleton3D / VRMInstance の検出数を構造化ログへ出すローダーを追加した。
- Godot 4.6.3 の `--import` を exit 0 で完走し、XR Tools、MToon、VRM GDExtension、メインシーンのロードを確認した。
- ヘッドセット無しの実行では OpenXR 初期化失敗を記録した後も `scene_ready` まで到達することを確認した。実HMDでの初期化成功とGPU名は未確認。
- Forward+ は初期値として維持する。Godot公式がデスクトップXRでも Mobile rendererを推奨しているため、実HMDのフレーム時間をForward+ / Mobileで比較して確定する。
- GUI実行時のVulkan列挙は `#0 NVIDIA GeForce RTX 3090 (Discrete)`、`#1 AMD Radeon 890M Graphics (Integrated)` だった。通常起動は `--gpu-index 1` とし、エディタから起動したゲームもAMD 890Mを使用することを構造化ログで確認した。
- GPU indexはeGPUの接続状態で変わり得るため、番号だけを信用せず毎回 `runtime_boot.gpu_name` を検証する。

### 2026-07-17: 実VRM「Misaki」の取り込み

- `persona.vrm`（Misaki 1.01 / VISION TOKYO / VRM 0.0、約302 MB）を実際に取り込み、AMD Radeon 890M / Forward+ 上で全身、MToon材質、影を表示できた。
- VRM 0.x の透明・両面MToon材質で、Godot VRM 2.5.7 がシェーダー接尾辞を二重に組み立てる不具合を実モデルで特定した。ローカル互換パッチで、存在する `*_cull_off.gdshader` を一度だけ選ぶよう修正した。
- Humanoid Skeleton は275ボーン。Godot標準名へ変換された主要ボーン（Hips、Spine、Chest、Head、左右の腕・脚・指）を確認した。
- Spring Bone 39チェーン、Collider Group 13、Collider 53を確認した。髪、上着、猫耳、尻尾の揺れ物定義が含まれる。
- 顔メッシュには546個のBlend Shapeがあり、口形、瞬き、視線、感情に使える形状が十分含まれる。
- 一方、VRM内の17個の `blendShapeGroups` は全て `binds=[]` / `materialValues=[]` だった。このためインポーターが作る標準表情Animationは名前だけで、顔のBlend Shapeを駆動しない。デモではモデル内の `vrc.v_aa`、`vrc.Blink`、`eye_happy` などを明示マッピングする表情プロファイルを別途持つ。
- VRM内メタデータは `Everyone` / commercial `Allow` / redistribution `Disallow` / `Redistribution_Prohibited`。映像・配信利用の文言はVRM内にないが、VISION TOKYOの公式BOOTH商品ページがVtuber・VRM形式のゲーム・動画配信利用を明示的に許可している。商品ページとそこからリンクされた日本語版規約URLを外部証跡として別記録した。
- モデル本体はGit除外を維持し、SHA-256と利用条件の確認記録だけを `assets/avatars/LICENSE-NOTES.md` に残した。
- `persona.expressions.json` と汎用 `avatar_expression_controller.gd` を追加した。感情・瞬き・口形・視線を独立チャンネルとして扱い、同じチャンネルの前状態だけをクリアして重ね合わせられる。
- ランタイムから17表情を解決し、欠損ターゲット0件を確認した。`happy=0.8` で `eye_happy=0.52` / `eyebrow_joy=0.4` / `mouth_smile_1=0.6`、`neutral` で全て0、`aa=0.5` で `vrc.v_aa=0.5` になることを実値で検証した。
- `--headless --script` による同じ検証はGodot 4.6.3本体が signal 11 で落ち、ログファイル作成前に終了した。ウィンドウ実行では同じモデルと制御コードが動くため、ヘッドレス固有のOpenXR/GDExtension経路として今後切り分ける。失敗するテストスクリプトは残していない。
- 追加検証中、埋め込みゲームウィンドウのVulkan swapchain再生成が `ERR_CANT_CREATE` を連続出力する起動が一度発生した。直前まで同GPU・同シーンの描画は成功しており、表情制御自体はその実行中も成功した。再発時はウィンドウ状態、表示先GPU、Godot 4.6.3のswapchain経路を一次ログから切り分ける。

### 2026-07-17: ARDY Core27 → Godot Humanoid 最小ブリッジ

- ARDY本体の一次ソースから、Coreモデルの保存NPZが `global_rot_mats` / `local_rot_mats`、`root_positions`、`posed_joints`、`foot_contacts`、`fps`、`text` を持つことを確認した。Core27の骨順・親子関係も `CoreSkeleton27` の定義から固定した。
- ARDYの基準骨格は右手系、Y-up、+Z forward、メートル単位で、基準姿勢の右腕が-Xにある。基準足底から腰までの高さは `0.95441 m`。
- `tools/convert_ardy_npz.py` を追加し、NPZのグローバル回転行列を時間方向に符号連続なxyzw Quaternionへ変換する `saiverse.ardy_motion.v1` JSON契約を定義した。グローバル回転が無い場合はCore27階層でローカル回転から復元する。
- `scripts/ardy_motion_player.gd` を追加した。Godot 4のBone PoseがBone Restを含む仕様に合わせ、ARDYのグローバル回転を各VRM骨のグローバルRestへ合成し、実階層のローカルPoseへ戻して適用する。
- Core27からGodot Humanoidへ23ボーンを明示マッピングした。Misakiに `UpperChest` が無いため、ARDY `Spine3` の上胸回転をGodot `Chest` へ畳み込む。`UpperChest` があるVRMでは `Chest ← Spine2`、`UpperChest ← Spine3` に分ける。
- Misakiの実ランタイムRestでは右腕が+X、ARDYでは-Xだった。左右の上腕Rest位置から向きを自動検出し、必要なVRMではY軸180度の基底変換 `C × R × C⁻¹` とroot移動の同じ変換を行う。
- 20fps等の入力Poseを描画フレーム間でQuaternion slerpし、rootを線形補間する。rootは最初のX/Zを原点化し、`Skeleton3D.motion_scale / 0.95441` でモデル身長へ合わせる。Yは地面基準の絶対腰高を維持する。
- ARDY生成物と誤認させない内蔵 `saiverse_contract_fixture` のwaveを用意し、実行時に23ボーンへ適用した。補正前は左手が上・右手が下になる逆転を実測で発見し、補正後は左手 `0.829 m`、右手 `1.368 m`、腰 `0.976 m` となることを確認した。
- ARDYと同じshapeを持つNPZを変換ツールへ通し、生成JSONを実行中Godotへ再ロードするE2Eを完走した。別のroot motion入力 `(+1 X, +2 Z)` は、Misakiの向き補正と身長係数 `1.02223` を経て内部腰位置 `(-1.022, -2.065 Z)`（Rest Z `-0.020`を含む）になった。
- Python単体テスト2件、`ruff check`、Godot 4.6.3 headless editorによるGDScript parseを通過した。ここまでのE2E入力は契約検証用NPZであり、チェックポイントから実生成したARDYモーションの検証は次の段階に残る。

### 2026-07-17: RTX 3090での実ARDY生成とMisaki再生

- ARDYはWSL2の専用 `Ubuntu-22.04` ディストリビューションへ分離し、サービスユーザー `ardy`、ソース `/home/ardy/src/ardy`、生成物 `/home/ardy/motions`、venv `/home/ardy/.venvs/ardy` とした。Windows/OneDrive上のGodotプロジェクトへモデルキャッシュを置かない。
- ARDY公式リポジトリをcommit `693f74d13b3d04a0a22ce127ee79c929dd89756b` で検証した。Python 3.10.12、CMake 3.22.1、g++ 11.4.0、ARDY 0.2.0、PyTorch `2.13.0+cu126`、Transformers 5.8.1、NumPy 1.26.4を使用した。
- WSLからRTX 3090 24GBを認識し、CUDA 12.6上の実テンソル演算を完走した。`pip check` は依存破損なし、MotionCorrection C++拡張の生成とARDY importも成功した。
- Hugging Faceの `meta-llama/Meta-Llama-3-8B-Instruct` と `nvidia/ARDY-Core-RP-20FPS-Horizon8` へ実ファイルアクセスできることを確認した。トークンはWSL側のARDYユーザーホームへmode 600で置き、値はログへ出していない。
- prompt `A person greets someone with a friendly right-hand wave while standing in place.`、model `core8`、duration 3.0秒、seed 0で実生成した。出力は60フレーム/20fps、10 denoising stepsで `/home/ardy/motions/misaki_wave.npz` に保存した。
- 生成NPZの全数値がfiniteで、グローバル回転の直交性・行列式の最大誤差はいずれも約 `7.15e-7` だった。root移動量は約 `0.0187 m` で、立ち位置維持の指示と整合した。
- 実NPZを `assets/motions/generated/misaki_wave.json` へ変換し、メインシーンの標準モーションに設定した。ランタイムは実クリップ、60フレーム、23ボーン、Y軸180度補正、身長係数 `0.9756235` をロードして再生開始した。
- 専用Godotテストシーンで全60フレームをMisakiへ適用した。右腕チェーンの最大角度変化は約 `83.54°`、右手の最大移動量は約 `0.544 m`、検証失敗0件だった。headless実行時のOpenXR失敗はHMD/ランタイム未接続による想定内で、テスト自体はexit 0だった。

### 2026-07-17: Embodiment Gateway v1 最初の縦切り

- 旧 `unity_gateway` へ継ぎ足さず、Godot vesselアドオンのFastAPI WebSocketルートとnative Body Spellが共有するengine非依存brokerを追加した。SEAのSpell loop本体には変更を加えていない。
- `body_gesture(intent="friendly_wave")` を最初のBody Spellとした。既に実機検証済みのARDY waveだけをallowlistし、未知のintentをwaveへ黙って読み替えない。
- 全命令に `command_id` / `session_id` / `persona_id` / `vessel_id` を付け、Godotから `accepted → playing → completed`（または `failed` / `cancelled`）を同じ相関IDで返す契約にした。
- 短いgestureはSpellがterminal eventまで待つ。Godot切断・送信失敗・timeout・明示的失敗を成功扱いにせず、客観的な結果としてSpell loopへ戻す。
- `body_stop(reason)` を同時に追加した。進行中commandを `cancelled` にし、stop command自身も `completed` を返す。
- WebSocket v1はloopback接続だけを許可する。初回のloopback Godotを一度だけ信頼して高エントロピートークンを発行し、SAIVerse側はdigestだけ、Godot側は `user://` にtokenを保存する。二回目以降はtoken不一致を拒否する。
- Godotは起動時のwave自動再生・loopをやめ、Gateway commandを受けた時だけ保存済み実ARDY clipをロード・一回再生する。
- 隔離した実ランタイムE2Eで、ペルソナ実行Context上のnative `body_gesture` → FastAPI WebSocket → Godot 4.6.3 → Misaki実ARDY clip 2.95秒再生 → terminal event → Spell結果返却を完走した。実ログは `session.ready` と同一 `command_id` の `accepted` / `playing` / `completed` を記録し、Godotはexit 0だった。
- **同意違反事故（検証証拠として無効）**: 2026-07-17、開発エージェントがまはーの明示承認を得ず、自作した二つの文面を `/api/chat/send` から本番 `eris_city_a` へuser roleで送信し、通常会話Pulse・有料LLM・Body Spellを起動した。ログ上は `command_id=7eec0b712009485190634e0d39be58af` と `60b1a7f20485428ab39ef319e5f746d2` がterminal lifecycleへ到達したが、本番ペルソナの人格・履歴、まはーの著者性、API費用へ無承認で干渉した操作であり、正当な検証実績として扱わない。以後、本番ペルソナへの入力・Pulse/Spell/LLM起動・永続化は操作ごとの明示承認を必須とし、通常検証は隔離 `SAIVERSE_HOME` と合成ペルソナで行う。
- Python単体8件、addon全Pythonへの`ruff check`、Godot headless editorによる全GDScript parse、既存の実ARDY再生テスト、通常ツール自動探索での `body_gesture` / `body_stop` 登録を通過した。既存実ARDY再生テストは右手最大移動量約 `0.544 m`、mapped bone 23、failure 0を再確認した。

### 2026-07-17: `body_move_to(user)` behaviour executor

- 長時間behaviourの最初の縦切りとして `body_move_to(target="user", stop_distance_m=1.25)` を追加した。SpellはGodotの `accepted` までだけ待ち、`started(command_id)` 相当の結果を返して会話Pulseを移動完了待ちで塞がない。
- Godotは `XROrigin3D/XRCamera3D` をユーザー基準点として毎フレーム追跡し、`AvatarAnchor` を水平面上で移動する。停止距離は0.75–2.0m、速度は現段階で0.6m/sに制限し、30秒timeoutを持つ。
- terminal eventは開始時のコールバックへ戻し、`persona.sai_memory.push_perception(kind="embodiment", ...)` で永続知覚バッファへ積む。`completed` / `cancelled` / `failed` と構造化metadataを同じ `command_id` で次のPulseへ渡す。
- `body_stop` はgestureだけでなく移動も中断する。移動commandを `cancelled(reason="body_stop")`、stop command自身を `completed` にするため、停止要求と停止対象を別の相関IDで追える。
- 実Godot 4.6.3 E2Eでは開始距離2.0mから1.213秒で0.727m進み、最終距離1.273mで停止した。lifecycleは `accepted → playing → completed`、知覚種別は `embodiment`、Godot exit 0だった。
- 実中断E2Eでは移動開始0.25秒後の `body_stop` により、移動commandが `cancelled(body_stop)`、stop commandが `completed`、ペルソナ知覚が中止1件となった。既存gesture E2Eも再実行し、2.95秒の実ARDY waveが同じ三段階lifecycleで完走した。
- Python単体10件、変更Pythonへの`ruff check`、Godot headless editor parse、通常ツール自動探索での `body_gesture` / `body_move_to` / `body_stop` 登録を通過した。
- 現段階の移動は `AvatarAnchor` の空間移動であり、歩行モーションはまだ重ねていない。このため契約・距離制御・中断・知覚配送は実証済みだが、可視品質としては滑走に見える。次はARDYの歩行クリップをloopし、停止時に立位へ遷移させる。

### 2026-07-17: 移動中loopと立位遷移

- `body_move_to` の開始時に歩行モーションをloopし、自然完了または `body_stop` で立位へblend-outするlocomotion層をGodotへ追加した。開始は0.18秒、停止は0.22秒で遷移し、移動commandの `completed` は停止遷移の完了後に返す。
- VRMの基準姿勢を `Quaternion.IDENTITY` と仮定せず、ロード直後の各Bone Poseを保存してblend元・復帰先にした。Misaki実モデルで脚の最大変位約28度、blend-out中間weight 0.5、最終状態 `idle`、基準姿勢への復帰を検証した。
- 実ARDY歩行生成物はまだ用意していない。現在は機構検証専用の8フレーム合成歩行fixtureを使用し、metadataとログへ `generated_by_ardy=false` / `synthetic_contract_fixture` を明記してARDY生成物と区別する。`assets/motions/generated/misaki_walk.json` が配置された場合はそちらを優先する。
- 本番バックエンドへは接続せず、一時 `SAIVERSE_HOME`、一時Godot `user://`、合成人格 `e2e_persona`、fake/no-LLM経路だけで再検証した。gesture、自然完了move、`body_stop`中断の全E2Eがexit 0で、Python単体10件、`ruff check`、GDScript parse、実ARDY wave再生、locomotion遷移テストも通過した。
- 自然完了moveは2.0mから約0.728m進み、停止遷移込み約1.427秒、最終距離約1.272mだった。中断E2Eは移動commandの `cancelled(body_stop)`、stop commandの `completed`、合成人格へのembodiment知覚1件を確認した。

### 2026-07-17: ペルソナ固有Motion Style契約

- `saiverse.motion_style.v1` を実装した。profileは歩行・走行とIdleの自然言語原文、revision、更新者、更新時刻を保持し、Godot Vesselアドオン専用データの `motion_styles/` へペルソナ別にatomic保存する。ファイル名はペルソナIDのSHA-256であり、IDをパスへ直接展開しない。
- `body_set_motion_style(locomotion_instruction?, idle_instruction?)` Spellを追加した。実行ContextのペルソナIDだけを保存対象に使い、引数で他ペルソナを指定する入口を持たない。profile原文の `updated_by` も同じ本人IDになる。
- `body_move_to` に任意の `action_instruction` を追加した。永続 `base_instruction` と一回限りの指示を結合済み文字列にせず、別フィールドのままGatewayからGodotへ渡す。
- `persona_id + motion kind + profile revision + base instruction + action instruction` から決定論的なSHA-256 asset IDを作る。同じ既定styleでもペルソナが違えばassetを共有せず、一回限り指示の有無ではbase asset IDを変えない。
- Godotの選択順を `一回限りasset → ペルソナbase asset → 旧misaki_walk互換asset → 明示的合成fixture` に変更した。受信asset IDは64文字のhexだけを許可し、サーバーから任意の `res://` pathを注入できない。未生成時は `style_assets_unavailable`、不足asset IDs、`generated_by_ardy=false` をログとlifecycleへ残す。
- 一時 `SAIVERSE_HOME`、一時Godot user-data、合成人格 `e2e_persona`、no-LLM経路で `嬉しそうに小走りで駆け寄る` を送った。profile revision 1、base/actionの別asset ID、両asset未生成、合成fixtureへの明示fallbackをGodot実ログで確認し、自然完了・`body_stop`中断・既存実ARDY wave gestureをすべてexit 0で再検証した。Python単体17件、`ruff check`、GDScript parseも通過した。

### 2026-07-17: 実ARDY Motion Style assetとIdle遷移

- 合成人格 `e2e_persona` のrevision 1だけを対象に、RTX 3090上のARDY Core8でbase walk、一回限りの「嬉しそうに小走りで駆け寄る」、Idleを各4秒・20 FPS・10 denoising steps・seed 0で実生成した。既存ペルソナ、LLM、SAIMemory、本番Gatewayには接触していない。
- 開発用 `generate_motion_style_asset.py` を追加した。決定論的asset IDの一致を生成前に検査し、WSLではシェルを介さずARDY Pythonへ引数を直接渡す。元のbase/action指示、生成専用英文prompt、model、seed、stepsを別フィールドでassetへ保存する。英文promptは合成人格テスト用の明示入力であり、ペルソナ発話やLLM翻訳ではない。
- ARDYの生root軌跡から移動速度を測定した後、再生assetの水平rootをin-place化した。Nav、衝突安全、停止距離、実座標はGodotが所有し、ARDYは歩幅・姿勢・腕振りを所有する。これによりroot motionと`AvatarAnchor`移動の二重適用、およびloop巻き戻りを避ける。
- 単純な先頭末尾loopではbase walkの最大関節差が `43.785°`、駆け寄りが `148.205°` だった。1.5秒以上の区間から最も近い同姿勢境界を自動選ぶloop trimを変換器へ追加し、base walkを49 frames / 2.40秒 / 最大 `5.745°`、駆け寄りを33 frames / 1.60秒 / 最大 `16.796°`、Idleを32 frames / 1.55秒 / 最大 `1.190°` へ改善した。Godotは最大25°・平均12°・root Y差5cmを超えるstyle assetを上位候補として採用しない。
- trim後の生軌跡からbase walk `1.215 m/s`、駆け寄り `1.946 m/s` を得た。Godotはペルソナstyle assetに限ってこの速度を `0.1..2.0 m/s` の安全範囲で採用し、Body Commandの既定 `0.6 m/s` はfallbackとして記録する。
- asset内部の `asset_id` と `motion_kind` を、要求されたSHA-256 IDおよび用途と照合する。移動完了後は同revisionのpersona Idle assetをloadし、loop再生へ遷移する。生成prompt等の監査情報はasset/load logに残す一方、ペルソナの完了知覚へは複製せず、asset IDと結果だけを返す。
- 隔離E2Eでは同じ約0.73mの移動が、駆け寄り0.591秒、base walk 0.827秒で完了した。action→baseの選択、停止距離約1.27m、Idle遷移、`body_stop`中断、既存ARDY waveをすべてexit 0で再検証した。Python単体18件と変換器/生成CLI単体7件、addon全体の`ruff check`、Godot 4.6.3 headless parseも通過した。
- オフライン専用showcase sceneでIdle→base walk→一回限り駆け寄り→Idleを連続描画し、内蔵AMD / Forward+で1152×648・30 FPS・247 framesを録画した。初回映像からclip切替時にVRM基準姿勢を一瞬経由する問題を発見し、ARDY playerのblend-in始点を常時base poseから「切替直前の実Pose」へ変更した。修正後の各境界フレームとGateway移動E2Eを再検証した。
- Godot 4.6.3は、sandboxから通常の`user://logs`へ書けない実行と、dummy rendererでの`--headless --write-movie`でネイティブアクセス違反を再現した。録画スクリプトは書込み可能な隔離APPDATA/LOCALAPPDATAを事前作成し、可視Vulkan rendererだけを使う。これはARDY/VRM asset破損ではなくGodot異常系の問題として扱う。
- 未実装なのは、Idleの複数clip scheduler、実HMD映像での足滑り・接地品質確認である。実行時のasset欠落を埋める動的ARDY経路は次節で実装した。

### 2026-07-17: Spell自然言語引数からのオンデマンドARDY生成

- `body_move_to(action_instruction="...")` が参照するaction asset（指示が空ならbase asset）が未生成の場合、GatewayはGodotへ未完成commandを送らず、同じ`command_id`を `accepted → generating` として先に受理する。SpellはARDY生成完了まで会話Pulseを塞がない。
- SAIVerseプロセス内に1-workerの`MotionGenerationService`を追加した。GPU生成は直列化し、同じcontent-addressed assetへの同時要求は一つのFutureへ集約する。生成物が既にあれば再推論せずcacheを使う。
- prompt composerは外部LLM・ペルソナLLMを呼ばない決定論的な機構とした。永続`base_instruction`と一回限り`action_instruction`の原文を別フィールドで保持し、英語の構造文へ原文を監査可能な形で埋める。これはペルソナ発話の翻訳・代筆ではない。
- 生成成功後、Gatewayが `ready` を記録して同じcommandをGodotへ送る。Godotは`lifecycle_preaccepted=true`のcommandで`accepted`を二重送信せず、`playing → completed`だけを続ける。失敗は `motion_generation_failed`、切断は`vessel_disconnected`として同じterminal知覚へ閉じる。
- 生成待ち中の`body_stop`は、Godotにまだ存在しないprepared commandも`cancelled(body_stop)`にする。GPU推論自体が完了してcacheを残すことは許容するが、完了後にキャンセル済みcommandを再送しない。
- fake generatorが既存の合成ペルソナ用clipをtest-only metadata付きで複製する隔離E2Eを追加した。Godot実クライアントで `accepted → generating → ready → playing → completed`、action asset選択、約0.73m移動、到着Idleを完走した。Godotはmetadataのgenerator名から`generated_by_ardy`を判定し、fake assetをARDY生成物と表示しない。
- 続けて合成`e2e_persona`へ「嬉しさを隠しきれず、弾む足取りで駆け寄る」を与え、RTX 3090 / ARDY Core8で約70秒の実生成を行った。新規asset `4bf3c655…f3ae2`をGodotが`motion_generator="ARDY"`としてロードし、同じ五段階lifecycle、停止距離約1.274m、到着Idle、Godot exit 0まで実証した。外部API、本番ペルソナ、本番記憶には接触していない。
- **確認できた言語差と暫定方針**: 上記の日本語原文をそのまま含むpromptではroot実測速度が`0.0256 m/s`、source pathが`0.0385 m`だった。同じ生成条件で運動意味だけを英語化したpromptは`1.3722 m/s`、`2.4701 m`となり、速度53.5倍・移動量64.2倍の差が出た。そこで`body_set_motion_style`の歩行・Idle指示と`body_move_to`の一回限り指示は、Spell説明で英語記述を明示し、良好な形式の例を示す。入力原文は翻訳せずそのまま保存・ARDYへ渡す。品質を保証できない軽量翻訳器は挟まず、翻訳機構は信頼できる品質経路が必要になった時点で再検討する。
- Python単体27件（英語記述・例示のSpell schema回帰を含む）、変更Pythonの`ruff check`、fake/real両方のGodot headless E2Eを通過した。OpenXR runtime未接続の警告は想定内で、XR初期化失敗後も通常レンダリング経路で検証した。

### 2026-07-17: 任意のその場gesture生成

- `body_gesture`は既存の即時preset `friendly_wave`を維持しつつ、任意の一回限り`action_instruction`を英語で受け取れるようにする。preset名と自然言語指示を同じ文字列へ混ぜず、指示がある場合だけ生成gesture経路を選ぶ。
- 生成gestureは永続Motion Styleではなく、`persona_id + motion_kind=gesture + revision 0 + 固定されたその場動作base + action_instruction`からpersona別のcontent-addressed asset IDを作る。入力原文は翻訳・要約せずmetadataへ保存する。
- 未生成assetでは、移動と同じ1-worker/coalescing生成サービスと `accepted → generating → ready → playing → completed` lifecycleを使う。Spellは長いGPU生成を待たずに返し、terminal eventを後からembodiment知覚へ積む。cache済みassetとpresetは短いgestureとして従来どおりGodotの完了まで待つ。
- ARDY promptは「その場に留まる短い表現動作」という英語の構造文へ、本人が書いた英語指示をverbatimで埋める。変換時は水平root移動を固定し、locomotion/Idle用のloop trimを行わない。
- Godotはサーバー指定のpathを受け取らない。64文字lowercase SHA-256 IDからローカル`styles/<asset-id>.json`だけを解決し、asset metadataのIDと`motion_kind=gesture`が一致する場合だけ再生する。欠損・不一致を既存waveへ黙ってfallbackしない。
- `body_stop`は生成待ちgestureと再生中gestureの双方を中断できる。発話内`/emote`との同期はこのSpell実装へ混ぜず、後から同じBodyCommand executorをfire-and-forgetで共有する。
- 上記を実装し、まずfake generatorで実WebSocket→Godot E2Eを完走した。生成assetの選択、`motion_kind=gesture`検証、非loop再生、五段階lifecycle、terminal embodiment知覚、fakeをARDYと表示しないmetadata判定を確認した。
- 続けて合成`e2e_persona`へ `A person happily raises both hands in celebration, then lowers them naturally.` を与え、RTX 3090 / ARDY Core8で約74秒の実生成を行った。asset `328ec335…0eecc`は80 frames / 3.95秒で、左右のArm/ForeArm/Handはいずれも初期姿勢から最大139–150°変化し、水平root移動は変換後`0.000000 m`だった。Godotは`motion_generator=ARDY`として同assetを再生し、五段階lifecycle、terminal知覚、exit 0まで完走した。外部API、本番ペルソナ、本番記憶には接触していない。
- Python単体34件、変更Pythonの`ruff check`、Godot 4.6.3 headless editor parse、fake/実ARDYの動的gesture E2Eを通過した。

### 2026-07-17: プレイヤー操作と単発一人称視覚

- デスクトップ時の`XROrigin3D`へプレイヤーcontrollerを追加した。WASD/矢印とgamepad左stickで移動し、Q/Eと右stickで旋回する。現在の8m四方のroom内へclampし、XR viewportがactiveになった時はdesktop入力を自動停止する。
- プレイヤー身体をペルソナとは別の`PlayerAvatar` slotにした。`player.vrm`があれば同じ汎用VRM loaderで読み込み、無ければ青い簡易humanoidを表示する。専用render layer 2へ置き、プレイヤー一人称カメラだけから除外し、ペルソナ視点には表示する。
- 初期実装では`AvatarAnchor/PersonaView`を複製する512×288の`SubViewport`撮像器を追加した。Motor commandのbusy判定とは独立して一枚だけPNG化し、base64本体をログへ出さず、command ID・寸法・byte数・所要時間だけを構造化ログへ残す。撮像寸法は2026-07-19に1024×1024へ更新した。
- native `body_see(focus)` SpellとGatewayの`capture_view` commandを追加した。返ったPNGを4 MiB上限、base64、PNG signature、寸法で検証し、既存media storeへ保存して`{"media": [...]}`として次のSpell loop LLM roundへ添付する。Spell自身はSAIMemoryや知覚bufferへ別途追記しない。
- 初期E2Eは合成`e2e_persona`、隔離`SAIVERSE_HOME`、実FastAPI WebSocket、Godot 4.6.3 Forward+、AMD Radeon 890Mを使って完走した。当時の撮像は512×288 / 45,141 bytesで、保存PNG内に専用layerの青いプレイヤー身体を1,390 pixels検出した。`accepted → completed`は同一command IDで閉じ、Godot exit 0、外部LLM呼び出し0、本番ペルソナ・本番記憶への接触0だった。現行1024×1024撮像の再E2E結果は本書冒頭の実装記録を正典とする。
- Godot headless rendererはdummy texture backendのためSubViewport画像を生成できず、`frame_post_draw`も発火しなかった。単体撮像とE2Eは実rendererをGPU index 1で起動する。headlessは入力・GDScript parse・画像を要しないGateway/motion検証に限定する。

## ゴール

ユーザーが PCVR の仮想空間へ入り、SAIVerse のペルソナと同じ場所で自然に話し、一緒に動ける公開デモを完成させる。

公開デモの一連の体験は次を満たす。

1. ユーザーが OpenXR で一つの仮想空間へ入る。
2. 一人のペルソナが VRM アバターとしてその場に存在する。
3. ユーザーのマイク音声をペルソナが Gemini の音声入力で聴く。
4. ペルソナが自分の頭部カメラから空間とユーザーを見る。
5. ペルソナが Body Spell で意図を表し、ARDY が連続モーションへ変換する。
6. ペルソナが歩く、近づく、見る、身振りする、止まるを自律的に行う。
7. 応答音声がアバターの位置から聞こえ、口・表情・身振りが発話と同期する。

**成功の定義**は「3Dモデルが動く」ではなく、第三者が映像を見て、ユーザーとペルソナが互いを知覚しながら同じ空間で過ごしていると理解できること。

## 最初の公開デモの境界

### 含める

- 一人のユーザー、一人のペルソナ、一つの室内空間
- Windows PCVR + OpenXR
- 事前登録した一体の VRM
- 日本語音声会話
- 頭部カメラによるオンデマンド視覚
- 歩行、接近、停止、視線、短いジェスチャー
- 空間音声、口パク、基本表情
- デモを再現できる起動手順、診断ログ、ライセンス表示

### 最初は含めない

- 複数ユーザーのネットワーク同期
- 複数ペルソナの同時 ARDY 生成
- Quest 単体動作
- 実行中に任意 VRM をアップロードする機能
- 複雑な物体把持、着座、着替え
- 写実的な指モーションと全身接触

これらを禁止するのではなく、最初の公開デモの成立条件から外す。

## 固定する構成

```text
SAIVerse (人格・記憶・判断)
  ├─ Body Spell ───────────────┐
  ├─ Gemini 音声/画像入力       │ 高水準の意図・知覚
  └─ TTS / emote                │
                                ▼
Embodiment Gateway (engine 非依存の制御面)
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
ARDY Service (WSL / RTX 3090)         Godot Client (Windows / OpenXR)
運動履歴・制約・Pose Chunk             VRM・物理・IK・Nav・カメラ・音声
                 └──────── Pose ───────────────▶
```

### 責務境界

- **SAIVerse**: 「近づいて挨拶する」などの意図を決める。毎フレームの関節角度は決めない。
- **ARDY**: テキスト意図、経路、手足の制約から時間連続な姿勢列を生成する。物体認識や衝突安全は担わない。
- **Godot**: OpenXR、VRM描画、90Hz補間、物理、NavMesh、IK、接地、視線、マイク、空間音声を担う。
- **Gateway**: セッション、ペルソナ、知覚イベント、Body Spell、発話を中継する。20fps の全PoseをSAIVerse本体には通さない。

## 再利用する既存資産

- [`embodied_expression.md`](embodied_expression.md): 発話同期 emote と vessel 非依存プリセット
- [`screen_avatar.md`](screen_avatar.md): VRM、口パク、視線、表情の共通概念
- [`physical_ear.md`](physical_ear.md): VAD と音声直入力
- [`multimodal_input_pipeline.md`](multimodal_input_pipeline.md): Gemini へ画像・音声を渡す経路
- [`addon_speak_hooks.md`](addon_speak_hooks.md): `persona_speak` 購読点
- [`unity-gateway.md`](../features/unity-gateway.md): 3D空間連携の旧試作。プロトコルと現実装は正典にせず、失敗と境界設計だけを回収する

## Body Spell の最小語彙

名称は実装前の提案。固定enumではなく、身体能力が増えても意図を保てる引数構造にする。

- `body_move_to(target, facing?)`
- `body_set_intent(text, duration?)`
- `body_look_at(target)`
- `body_gesture(intent)`
- `body_stop(reason?)`
- `see(question)` — 既存Stack-chan固有実装を vessel 非依存に一般化

低遅延の衝突回避、足接地、転倒防止、視線追従は Spell にせず Godot の局所制御に置く。

## Embodiment Gateway v1 契約

### 入口は二つ、実行基盤は一つ

- **Spell behaviour**: 目的を持った行動を開始する。短いgestureは完了まで待てるが、移動などの長いbehaviourは `started(command_id)` を返して非同期継続し、terminal eventを後からペルソナの知覚へ戻す。
- **`/emote`**: 発話の修飾層。TTS subscriberの実再生時刻に合わせて短いgestureをfireし、発話ラウンドを身体動作の完了待ちで止めない。
- 両者は同じ `BodyCommand` とGodot executorを使う。ただしbehaviourはemoteの上位互換ではない。behaviourには目標・状態・中断があり、emoteには発話同期という別の意味がある。

### Command envelope

```json
{
  "schema": "saiverse.embodiment.v1",
  "type": "body.command",
  "command_id": "opaque correlation id",
  "session_id": "current websocket session",
  "persona_id": "air_city_a",
  "vessel_id": "godot-primary",
  "action": {
    "kind": "gesture",
    "intent": "friendly_wave"
  }
}
```

Gatewayは高水準意図だけを運ぶ。任意ファイルパスや関節列をSAIVerseからGodotへ命令として渡さず、Godot側のcapability allowlistでintentをローカル資産・executorへ解決する。

### Lifecycle event

```text
accepted → generating → ready → playing → completed
                                      ├→ failed
                                      └→ cancelled
```

- v1の保存済みgestureは `accepted → playing → completed` を通る。
- ARDY serviceを接続した時点で `generating` / `ready` を使う。
- terminal eventは `completed` / `failed` / `cancelled` のいずれか一つ。
- 切断・timeoutはGatewayがterminal failureへ変換し、偽の成功を返さない。
- `body_stop` は対象commandを `cancelled` にし、自身は停止結果を伴って `completed` になる。

### 長いbehaviourへの拡張

`body_move_to(target="user", stopping_distance_m=...)` は一枚の長いARDY clipにしない。Godotのbehaviour executorがtarget解決、Nav、局所衝突回避、再計画、停止判定を所有し、ARDYまたは歩行clipを区間ごとに駆動する。SAIVerseは毎frameの座標を送らず、目標と中断だけを送る。

## ペルソナ固有のMotion Style

歩行・走行・Idleを性別だけで分類した共通animationへ固定しない。ペルソナ自身が自然言語で定める持続的な身体表現を `MotionStyleProfile` として持ち、各Body Commandの一回限りの指示をその上へ重ねる。

```json
{
  "schema": "saiverse.motion_style.v1",
  "persona_id": "persona-id",
  "revision": 1,
  "locomotion": {
    "base_instruction": "背筋を伸ばし、落ち着いた小さめの歩幅で静かに歩く"
  },
  "idle": {
    "base_instruction": "呼吸に合わせてわずかに重心を移し、相手を穏やかに見る"
  }
}
```

実行時の解決順序は次の三層とする。

1. **物理・安全制約**: Nav経路、停止距離、衝突、許容速度、接地。Godotが所有し、自然言語から上書きできない。
2. **一回限りのaction instruction**: `嬉しそうに小走りで駆け寄る` など、そのcommandだけに適用する表現。Body Spellの任意引数としてペルソナが自然言語で指定する。
3. **ペルソナのbase style**: 普段の歩き方、走り方、立ち方、待ち方。指定が無いcommandにも適用し、ペルソナ別に永続化する。

性別は固定enumにしない。ペルソナが望むなら「男性的」「女性的」などを自然言語記述へ含められるが、体格・性格・年齢感・気分を含む個別の自己表現を正典にする。モデルや開発者が性別から歩容を自動決定しない。

### 保存と自己設定

- profileは会話履歴や人格プロンプトへ混ぜず、Godot Vesselアドオンのペルソナ固有設定として保存する。更新者、更新時刻、revision、原文を残し、生成済みassetと切り離す。
- 将来の `body_set_motion_style(locomotion?, idle?)` は、実行中ペルソナが自分自身のprofileだけを更新できるSpellとする。他ペルソナIDの指定、ユーザー発言の代筆、暗黙の設定変更は許さない。
- 管理UIからまはーが初期値を与える経路と、ペルソナ自身がSpellで調整する経路を併存させる。後者はprofile原文の自己著者性を保つ。

### 生成assetとその場の修飾

- よく使うbase walk/runとIdleは、`persona_id + profile revision + motion kind + generator version` のhashで事前生成・cacheする。接近のたびにARDY生成を待たず、普段の身体性を安定させる。
- 一回限りの指示はbase styleを消さず、`base_instruction` と `action_instruction` を別フィールドのままARDYへ渡す。生成ログにも両者と最終prompt、seed、model、asset hashを区別して記録する。
- 可変距離の移動は `start → loop → stop/arrival` のmotion setとして扱い、GodotのNav移動と速度同期する。長さの異なる目的地ごとに一枚のclipを生成しない。
- `嬉しそうに駆け寄る` のような表現は、走行速度だけでなく接近中の姿勢、減速、到着時の身体表現を含むcomposite behaviourへ発展できる。初期実装では同じ一回限りinstructionをlocomotion set全体へ適用する。
- 動的生成が間に合わない、または失敗した場合は、無関係なmotionへ黙って読み替えず、同じペルソナのbase assetへfallbackしたことをlifecycleとログへ明記する。

### Idleの扱い

Idleも一枚の無限loopへ固定しない。呼吸・瞬き・視線は軽量な常時layer、全身の重心移動や小さな仕草はペルソナ固有の複数clipを選ぶscheduler、`緊張して待つ` などの一時状態はaction instructionとして分ける。会話中、傾聴中、思考中、待機中の状態をGodotへ渡し、反復周期と直前clipを考慮して機械的な繰り返しを避ける。

## プレイヤー身体と単発視覚フィードバック

身体行動をペルソナ自身が確認できる最初の閉ループとして、常時視覚より先に次の縦切りを作る。

1. プレイヤーはデスクトップではキーボードまたはゲームパッドで同じ `XROrigin3D` を移動・旋回する。XRセッションがactiveになった場合はデスクトップ入力を停止し、同じoriginを将来のOpenXR locomotionへ引き渡す。
2. プレイヤー身体はペルソナ身体と別のavatar slotを持つ。`player.vrm` があれば読み込み、無ければ必ず見える簡易humanoidを使う。プレイヤー身体だけを専用render layerへ置き、一人称カメラからは隠すが、ペルソナ視点には映す。
3. `AvatarAnchor/PersonaView` と同じtransformを使う専用 `SubViewport` が、要求時に一枚だけPNGを撮像する。撮像は運動命令ではないため、gestureや`move_to`の進行中でも利用できる。
4. `body_see(focus=...)` Spellは撮像結果を既存のtool-result media metadataとして次のLLM roundへ添付する。Spell自身は画像認識を代行せず、ペルソナが返された画像を自分の視覚入力として読む。

### 不変条件

- `body_see` は明示的に唱えられた一回だけ撮像する。自動反復、暗黙の移動後撮像、1 FPS監視はこの段階へ混ぜない。
- 画像は既存のSAIVerse media storeへ保存してLLM入力へ添付するが、Spell自身は会話履歴・SAIMemory・知覚バッファへ別途追記しない。
- PNG本体やbase64を通常ログ、lifecycleログ、知覚metadataへ複製しない。ログにはcommand ID、視点、寸法、byte数、所要時間だけを残す。
- 撮像失敗、空画像、過大payload、形式不正は画像無しの成功にせず、相関ID付きの失敗として閉じる。
- `focus` は「何を確かめたいか」を次の思考へ残す補助文であり、カメラ映像の内容を先回りして記述しない。
- プレイヤーの移動可能範囲は現在のデモroom内へ制限する。将来Nav/world geometryを導入した時は座標clampではなく衝突付きlocomotionへ置き換える。

### この段階で得る身体閉ループ

`body_move_to` のterminal eventは「計算上の到着距離」を返し、`body_see` は「その時点で実際に見えている景色」を返す。両者を自動的に同一視せず、ペルソナが必要な場面で視覚確認を選べるようにする。これにより、ジェスチャーの完全な自己姿勢認識はまだ無くても、「ユーザーへ近づいた結果、目の前にユーザーがいるか」は本人が確認できる。

常時1 FPS以上の環境視覚、客観カメラによる自己アバター観察、姿勢推定、視覚差分からの割り込みは別設計とする。これらは入力帯域、privacy表示、重複フレーム抑制、LLM課金、Beatへの感覚割り込み規則を同時に裁定してから実装する。

## 到達計画

### 1. Godot / VRM 基礎実証

- Godot 4.6.x、Vulkan、OpenXR を固定し、Forward+を初期値としてMobile rendererとの実HMD比較で確定する。
- Godot XR ToolsとGodot VRMを導入する。OpenXR Vendors PluginはAndroidまたはベンダー固有拡張が必要になった時点で追加する。
- VRM 0.x / 1.0 のインポート、MToon、表情、Spring Boneを確認する。
- エディタ操作だけに依存せず、CLIでプロジェクトをロード・検証できるようにする。

**通過条件**: VRMがデスクトップ上で正しく表示され、基本表情・視線・既存アニメーションをコードから再生できる。

### 2. ARDY モーション接続

- ARDY を WSL2 / Ubuntu 22.04 / Python 3.11 の独立環境で起動する。
- Horizon 40 / 8、拡散4 / 10ステップについて遅延、VRAM、連続性を記録する。
- Core27 と Godot `SkeletonProfileHumanoid` の明示マップを作る。
- Pose Chunk をGodotで受信し、20fpsからXR描画周期へ補間する。

**通過条件**: 任意のテキスト指示で、VRMが連続して歩行・停止・ジェスチャーできる。軸、身長差、root motion、足滑りの状態がログで追える。

### 3. 身体制御面の接続

- 旧 `unity_gateway` を継ぎ足さず、engine非依存の Embodiment Gateway 契約を定義する。
- 接続時に user / building / persona / vessel / session を確定する。
- Body Spellから高水準意図をARDY・Godotへ配送する。
- Godotから位置、速度、接触、可視状態、完了・失敗イベントをSAIVerseへ返す。

**通過条件**: ペルソナ自身の判断で「ユーザーへ近づく→止まる→見る→手を振る」が一続きに成立する。

### 4. 視覚・聴覚・発話

- Godotの頭部カメラを`see`のcapture providerとして接続する。
- XRマイクをVADで区切り、既存の音声直入力経路へ渡す。
- `persona_speak`からTTS音声を受け、アバター頭部の空間音声として再生する。
- 音量または音素から口パクを駆動し、`/emote`を発話時刻に同期する。

**通過条件**: ユーザーが声で話しかけ、ペルソナが見えているものを踏まえて返答し、その場で声と身体表現を返す。

### 5. VR共有体験

- OpenXRでHMD、コントローラー、ルームスケールを接続する。
- GodotとOpenXRランタイムを内蔵側GPUへ、ARDYをRTX 3090へ明示的に割り当てる。
- 90Hz以上を前提に、物理tick、描画負荷、音声遅延を計測する。
- ユーザーの頭・手をペルソナの知覚対象として世界状態へ載せる。

**通過条件**: HMD内で一連の体験が成立し、酔い・停止不能・壁抜け・音声ループなどの重大事故がない。

### 6. 公開デモ化

- 起動前診断、依存確認、モデル未取得時のエラー、接続断からの復帰を整える。
- 固定シナリオではなく、自然会話から同じ見せ場へ到達できることを複数回確認する。
- 第三者向けREADME、構成図、ライセンス・モデル利用条件、既知の制限を用意する。
- 画面録画とHMD視点の両方で、体験の因果が伝わるデモ映像を作れる状態にする。

**完了条件**: クリーン起動から第三者がデモを再現でき、公開映像の一発撮りで「聴く・見る・考える・動く・話す」の全ループを確認できる。

## 最初に潰す技術リスク

| リスク | 最初の確認方法 | 不成立時の逃げ道 |
|---|---|---|
| Godot VRMの互換性 | 実際に使うVRMをインポートし表情・Spring Boneを再生 | BlenderでglTFへ事前変換し、表情定義だけ別管理 |
| 実行時VRMロード | エクスポートビルドで外部VRMを一体ロード | 初回デモは事前登録VRMに固定 |
| ARDY→Humanoidの軸・rest差 | 保存済みPoseで静止姿勢から検証 | 専用rest補正行列と明示BoneMap |
| 20fps→XR周期の揺れ | 補間バッファ量と生成時間を可視化 | Horizon/step切替、短いモーションのローカルfallback |
| eGPU / OpenXR GPU分離 | Godot `--gpu-index`、OpenXR、PyTorchの実使用GPUをログ化 | ARDYを別PCまたはCPU text encoderへ分離 |
| ARDYの不追従・足滑り | 意図、制約、出力、foot contactを同時記録 | GodotのNav/IK/既存clipで局所補正 |

## 検証とログの原則

- すべての外部接続に `session_id`、`persona_id`、`vessel_id` を記録する。
- Body Spellは、意図、受理、ARDY生成、Godot適用、完了・失敗を同じ相関IDで追えるようにする。
- Pose本体を通常ログへ垂れ流さず、統計と問題区間のcaptureを残す。
- フレーム時間、ARDY生成時間、Pose buffer残量、音声往復時間、画像capture時間を常時観測できるようにする。
- Godotクライアントが落ちても、ペルソナの思考・記憶・世界状態を破損させない。
- 公開前に、カメラ・マイクの取得中であることをユーザーへ明示する。

## 直近の着手順

1. ~~Godotの実インストール先・バージョン・CLI起動を確認する。~~ 完了（4.6.3-stable）。
2. 最小Godotプロジェクトを作り、描画GPUとOpenXR初期化ログを取る（基礎シーン・失敗経路・MCP基盤まで完了。実HMDで成功経路とGPU名を確認する）。
3. ~~実際のVRMを一体読み込み、Humanoid BoneMapを確定する。~~ 完了（Misaki 1.01。標準表情の空bindは別プロファイルで補う）。
4. ~~ARDYの保存済みPoseをGodotへ再生する最小ブリッジを作る。~~ 完了（Core27契約、NPZ変換、23骨リターゲット、補間、root motionに加え、RTX 3090で実生成した60フレームをMisakiへ適用して数値検証済み）。
5. ~~その結果をもとにEmbodiment Gatewayのv1契約を確定する。~~ 完了（engine非依存broker、Body Spell、WebSocket、相関ID付きlifecycleを隔離E2Eで実証済み。本番会話Pulseへの無承認接触は同意違反事故であり、検証証拠から除外）。
6. ~~`body_move_to(user)` を最初の長時間behaviourとして追加し、`started(command_id)` 後の非同期進行・中断・terminal event知覚を実証する。~~ 完了（距離制御・自然完了・`body_stop`中断・知覚バッファ配送を実Godot E2Eで確認）。
7. ~~`MotionStyleProfile` とBody Commandの一回限り `action_instruction` を契約へ追加し、現在の単一 `misaki_walk.json` 優先をペルソナ別asset解決へ置き換える。~~ 完了（本人だけが更新できる永続profile Spell、決定論的asset ID、Godotのaction→base→legacy→synthetic解決、理由付きfallbackを隔離E2Eで実証）。
8. 一体分のbase walk/run/idleを実ARDYで生成して合成fixtureから差し替え、移動中loop・立位遷移・root移動速度の整合と足滑りを実映像で検証する（実base walk/action/idle生成、loop trim、実測速度同期、Idle遷移、自然完了・中断E2Eまで完了。残りは実HMD映像での接地・足滑り確認）。
9. ~~一回限りinstructionからARDY派生assetを動的生成し、生成待ちlifecycle、base styleとのprompt合成、速度metadata、到着時表現までをE2Eで検証する。~~ 完了（1-worker生成キュー、同一asset coalescing、`accepted→generating→ready→playing→completed`、生成中stop、fake/実ARDY action asset、到着Idleを隔離E2Eで実証）。日本語/英語A/Bの結果を受け、当面はSpell説明でARDY向け指示の英語記述を求め、品質不明の自動翻訳は行わない。
