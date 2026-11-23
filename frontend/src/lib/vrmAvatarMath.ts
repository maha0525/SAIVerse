/** ルームビューの 3D アバター (docs/intent/screen_avatar.md 設計 3) の、React にも three にも
 * 依らない決まり。描く側 (lib/vrmAvatarEngine.ts) と部品 (components/VrmAvatar.tsx) が使い、
 * scripts/test-vrm-avatar-math.cjs が実物の関数で確かめる。
 *
 * ⚠ このファイルから three / @pixiv/three-vrm を import しないこと。VrmAvatar.tsx は
 * RoomView.tsx から静的に読まれるので、ここで three を読むと、VRM を持たないペルソナしか
 * いない画面にも three が載る (必ず守ること 9)。 */

/** 映す範囲。'upper' は腰から上 (placement が 'sidebar' と 'overlay')、'full' は全身 ('column')。 */
export type AvatarFramingMode = 'upper' | 'full';

/** 読み込みに失敗した理由。画面には、理由ごとに決まった文言を出す (VrmAvatar.tsx)。
 * - 'webgl': WebGL が使えない (描くための canvas を作れなかった)
 * - 'http': VRM を返す API が失敗を返した (detail に状態番号)
 * - 'network': VRM のファイルを受け取り切れなかった
 * - 'parse': 受け取ったファイルを glTF として読めなかった
 * - 'notVrm': glTF としては読めたが、VRM の情報が無かった
 * - 'contextLost': 描いている途中で WebGL の描画が失われた
 * - 'engine': 3D のライブラリそのものを読み込めなかった */
export type VrmAvatarFailure = 'webgl' | 'http' | 'network' | 'parse' | 'notVrm' | 'contextLost' | 'engine';

export type VrmAvatarStatus =
    | { phase: 'idle' }
    | { phase: 'loading'; progress: number | null }
    | { phase: 'ready' }
    | { phase: 'failed'; reason: VrmAvatarFailure; detail?: string };

/** 一つの部品 (借り手) から見た 3D アバターの様子。holding が true の間だけ、
 * その部品の枠の中に canvas があり、3D が見えている。 */
export interface VrmAvatarView {
    holding: boolean;
    status: VrmAvatarStatus;
}

export interface AvatarBorrowerVisibility {
    id: number;
    visible: boolean;
    /** 最後に見えるようになった順番 (大きいほど新しい)。見えていない間の値は使わない。 */
    visibleSince: number;
}

/** canvas を持つ借り手を決める。
 * - 画面に見えている借り手のうち、一番後から見えるようになったもの (同じなら後から借りたもの)。
 * - 見えている借り手が一人もいなければ、今の持ち主がまだ借りているならそのまま (canvas を
 *   動かさない)。いなければ null。
 * 見えている借り手が持ち主を替えるのは、新しく見えるようになったときだけ。見えていない
 * 借り手が増えたり減ったりしても、見えている持ち主は替わらない。 */
export function chooseAvatarHolder(
    borrowers: readonly AvatarBorrowerVisibility[],
    currentHolderId: number | null,
): number | null {
    let best: AvatarBorrowerVisibility | null = null;
    for (const b of borrowers) {
        if (!b.visible) continue;
        if (!best || b.visibleSince > best.visibleSince
            || (b.visibleSince === best.visibleSince && b.id > best.id)) {
            best = b;
        }
    }
    if (best) return best.id;
    if (currentHolderId !== null && borrowers.some(b => b.id === currentHolderId)) return currentHolderId;
    return null;
}

/** 借り手がいなくなった VRM を、捨てずに持っておく時間。同じ部屋で次に喋るときや、
 * 狭い画面でログに重ねるルームビューがマウントし直されたときに使い回す。 */
export const VRM_KEEP_AFTER_RELEASE_MS = 3 * 60 * 1000;

/** 描画の解像度に使う devicePixelRatio の上限。 */
export const MAX_AVATAR_PIXEL_RATIO = 2;

export function avatarPixelRatio(devicePixelRatio: number | undefined): number {
    const ratio = typeof devicePixelRatio === 'number' && Number.isFinite(devicePixelRatio) && devicePixelRatio > 0
        ? devicePixelRatio
        : 1;
    return Math.min(ratio, MAX_AVATAR_PIXEL_RATIO);
}

export interface Vec3 { x: number; y: number; z: number }

/** humanoid のボーンの、立ち姿での位置 (ワールド座標、メートル)。
 * 体の正面が +Z を向いている前提 (VRM 0.x は VRMUtils.rotateVRM0 で向きを揃えた後)。 */
export interface AvatarBonePositions {
    hips: Vec3;
    head: Vec3;
    leftFoot?: Vec3 | null;
    rightFoot?: Vec3 | null;
    leftUpperArm?: Vec3 | null;
    rightUpperArm?: Vec3 | null;
}

export interface AvatarCameraFraming {
    /** カメラの位置 */
    position: Vec3;
    /** カメラが向く点 */
    target: Vec3;
}

/** カメラの縦の画角 (度)。望遠寄りにして、顔の遠近の歪みを抑える。 */
export const AVATAR_CAMERA_FOV_DEG = 30;

/** 頭のボーン (首の付け根あたり) から頭のてっぺんまでを、腰から頭のボーンまでの長さの
 * 何倍とみなすか。髪の盛りの分を少し多めに見る。 */
const HEAD_TOP_RATIO = 0.45;
/** 'upper' で、腰のボーンからどれだけ下まで映すか (腰から頭のボーンまでの長さの倍率)。 */
const UPPER_BOTTOM_BELOW_HIPS_RATIO = 0.25;
/** 'full' で、足首のボーンから足の裏 (と床) までの余裕 (腰から足首までの長さの倍率)。 */
const FULL_BOTTOM_BELOW_FEET_RATIO = 0.12;
/** 上下の余白 (映す高さの割合)。 */
const VERTICAL_MARGIN_RATIO = 0.05;
/** 'full' の下の余白 (映す高さの割合)。ルームビューは枠の下寄りに名前の札を重ねて出すので
 * (RoomView.module.css の .figureArea と .nameTag)、足が札に隠れないよう、上より広く取る。 */
const FULL_BOTTOM_MARGIN_RATIO = 0.13;

/** 映す範囲から、カメラの位置と向きを決める。範囲の縦が枠の縦に収まるように、また
 * 体の幅 (腕を下ろした肩幅と手) が枠の横に収まるように、遠いほうの距離を採る。
 *
 * 体の幅に合わせてカメラを引いたときは、映る縦の範囲が、映したい範囲より広くなる。
 * そのときの余りは、'upper' では下へ回し (頭の上の余白はそのままにして、体が枠の下端まで
 * 続いて切れる)、'full' では上へ回す (足先は枠の下端の少し上のまま)。 */
export function computeAvatarFraming(
    bones: AvatarBonePositions,
    mode: AvatarFramingMode,
    aspect: number,
    fovDeg: number = AVATAR_CAMERA_FOV_DEG,
): AvatarCameraFraming {
    const { hips, head } = bones;
    const torso = Math.max(head.y - hips.y, 0.05);
    const top = head.y + torso * HEAD_TOP_RATIO;
    const feet = [bones.leftFoot, bones.rightFoot].filter((v): v is Vec3 => !!v);
    const footY = feet.length > 0 ? Math.min(...feet.map(f => f.y)) : hips.y - torso * 1.6;
    const legs = Math.max(hips.y - footY, 0.05);
    const bottom = mode === 'full'
        ? footY - legs * FULL_BOTTOM_BELOW_FEET_RATIO
        : hips.y - torso * UPPER_BOTTOM_BELOW_HIPS_RATIO;
    const span = Math.max(top - bottom, 0.1);
    const marginTop = span * VERTICAL_MARGIN_RATIO;
    const marginBottom = span * (mode === 'full' ? FULL_BOTTOM_MARGIN_RATIO : VERTICAL_MARGIN_RATIO);
    const height = span + marginTop + marginBottom;

    const shoulderWidth = bones.leftUpperArm && bones.rightUpperArm
        ? Math.abs(bones.leftUpperArm.x - bones.rightUpperArm.x)
        : torso * 0.8;
    // 腕を下ろすと、手は肩の付け根より少し外に出る。
    const halfWidth = Math.max(shoulderWidth, 0.05) * 0.95;

    const tanHalf = Math.tan((fovDeg * Math.PI / 180) / 2);
    const safeAspect = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
    const distanceForHeight = (height / 2) / tanHalf;
    const distanceForWidth = (halfWidth * 1.1) / (safeAspect * tanHalf);
    const distance = Math.max(distanceForHeight, distanceForWidth);
    // 実際に映る縦の範囲の半分。縦で決まったときは height / 2 と同じになる。
    const visibleHalfHeight = distance * tanHalf;
    const centerY = mode === 'full'
        ? (bottom - marginBottom) + visibleHalfHeight
        : (top + marginTop) - visibleHalfHeight;

    const centerX = (bones.leftUpperArm && bones.rightUpperArm)
        ? (bones.leftUpperArm.x + bones.rightUpperArm.x) / 2
        : hips.x;
    const target = { x: centerX, y: centerY, z: hips.z };
    return { position: { x: centerX, y: centerY, z: hips.z + distance }, target };
}

/** 腕を下ろす角度 (ラジアン)。真横 (T ポーズ) から真下へ向けて、これだけ回す。
 * 真下 (π/2) まで下ろすと、腰回りの広い服に腕がめり込むので、少し手前で止める。 */
export const ARM_LOWER_ANGLE_RAD = 1.25;

/** 正規化された humanoid の上腕を、Z 軸まわりに何ラジアン回すと腕が下りるか。
 * 正規化されたボーンの軸は、VRM 1.0 では体の正面が +Z・左手が +X、VRM 0.x では
 * 体の正面が -Z・左手が -X にある (VRMUtils.rotateVRM0 は vrm.scene を回すだけで、
 * ボーンの中の向きはそのまま)。だから同じ「下ろす」でも、回す向きが逆になる。 */
export function restArmRotationZ(side: 'left' | 'right', metaVersion: string | undefined, angle: number = ARM_LOWER_ANGLE_RAD): number {
    const vrm0 = metaVersion === '0';
    const sign = (side === 'left') !== vrm0 ? -1 : 1;
    return sign * angle;
}

/** 喋っている間の、口の開き (0〜1) を時刻 (秒) から決める。声の大きさは使わない。
 * 一秒に 4.5 回ほど開け閉めし、間隔と開き方に、周期の揃わない揺らぎを足して、
 * 同じ動きの繰り返しに見えないようにする。閉じている時間 (値が 0) も毎回作る。 */
export function mouthOpenness(timeSec: number): number {
    const t = timeSec;
    const tau = Math.PI * 2;
    const phase = t * tau * 4.5
        + 0.9 * Math.sin(t * tau * 0.7)
        + 0.5 * Math.sin(t * tau * 1.9 + 1.3);
    const amplitude = 0.45
        + 0.45 * (0.5 + 0.5 * Math.sin(t * tau * 0.37 + 0.4)) * (0.5 + 0.5 * Math.sin(t * tau * 1.13));
    const value = Math.max(0, Math.sin(phase)) * amplitude;
    return Math.min(1, Math.max(0, value));
}

/** current を target へ、時定数 timeConstantSec で近づける (dt 秒分)。 */
export function smoothToward(current: number, target: number, dtSec: number, timeConstantSec: number): number {
    if (!(dtSec > 0)) return current;
    if (!(timeConstantSec > 0)) return target;
    const k = 1 - Math.exp(-dtSec / timeConstantSec);
    return current + (target - current) * k;
}

/** 口の開きを target へ近づける速さ (秒)。開くのは速く、閉じるのは少しゆっくり。 */
export const MOUTH_OPEN_TIME_CONSTANT_SEC = 0.04;
export const MOUTH_CLOSE_TIME_CONSTANT_SEC = 0.08;

/** 声の音量 (RMS) がこの値以下なら、口を閉じる。 */
export const VOICE_LEVEL_SILENT_RMS = 0.015;
/** 声の音量 (RMS) がこの値以上なら、口をいっぱいに開く。
 * 二つの値は、人の声の RMS がおおよそ 0.03〜0.2 に入るという見立てで置いた仮の値で、
 * 実機で調整する前提。 */
export const VOICE_LEVEL_FULL_RMS = 0.15;

/** 声の音量 (RMS、0〜1) から口の開き (0〜1) を決める。VOICE_LEVEL_SILENT_RMS 以下は 0、
 * VOICE_LEVEL_FULL_RMS 以上は 1、その間はまっすぐ。数でない値は 0。 */
export function mouthOpennessFromVoiceLevel(rms: number): number {
    if (!Number.isFinite(rms) || rms <= VOICE_LEVEL_SILENT_RMS) return 0;
    if (rms >= VOICE_LEVEL_FULL_RMS) return 1;
    return (rms - VOICE_LEVEL_SILENT_RMS) / (VOICE_LEVEL_FULL_RMS - VOICE_LEVEL_SILENT_RMS);
}

/** 口の開きの目標 (0〜1) を決める。
 * 1. 声が鳴っていて (voiced)、音量が読めた (voiceLevel が数) → 音量から決める。
 * 2. そうでなく、声が鳴っているか喋っている (speaking) → 時間で開け閉めする (mouthOpenness)。
 *    音量を読めないブラウザや、声の無い発話 (文字だけ) のとき。
 * 3. どちらでもない → 0 (閉じる)。 */
export function decideMouthTarget(input: {
    voiced: boolean;
    speaking: boolean;
    voiceLevel: number | null;
    timeSec: number;
}): number {
    if (input.voiced && typeof input.voiceLevel === 'number' && Number.isFinite(input.voiceLevel)) {
        return mouthOpennessFromVoiceLevel(input.voiceLevel);
    }
    if (input.voiced || input.speaking) return mouthOpenness(input.timeSec);
    return 0;
}

/** まばたきの長さ (秒): 閉じる → 閉じたまま → 開く。 */
export const BLINK_CLOSE_SEC = 0.06;
export const BLINK_HOLD_SEC = 0.03;
export const BLINK_OPEN_SEC = 0.09;
export const BLINK_TOTAL_SEC = BLINK_CLOSE_SEC + BLINK_HOLD_SEC + BLINK_OPEN_SEC;

/** 次のまばたきまでの間 (秒)。random は 0 以上 1 未満 (Math.random())。
 * 2〜6 秒の間で不規則にし、たまに (random が小さいとき) すぐ続けてもう一度まばたく。 */
export function nextBlinkDelaySec(random: number): number {
    const r = Math.min(Math.max(Number.isFinite(random) ? random : 0.5, 0), 0.999999);
    if (r < 0.1) return 0.25 + r * 2;  // 0.25〜0.45 秒: 二度続けるまばたき
    return 2 + ((r - 0.1) / 0.9) * 4;
}

/** まばたきが始まってからの時間 (秒) での、目の閉じ具合 (0 = 開いている、1 = 閉じている)。 */
export function blinkWeight(elapsedSec: number): number {
    if (!(elapsedSec > 0)) return 0;
    if (elapsedSec < BLINK_CLOSE_SEC) return elapsedSec / BLINK_CLOSE_SEC;
    if (elapsedSec < BLINK_CLOSE_SEC + BLINK_HOLD_SEC) return 1;
    const opening = elapsedSec - BLINK_CLOSE_SEC - BLINK_HOLD_SEC;
    if (opening < BLINK_OPEN_SEC) return 1 - opening / BLINK_OPEN_SEC;
    return 0;
}

/** 体とは別の armature の bone を、体の humanoid の bone の組にしてよい、rest の world 位置の
 * 離れ具合の上限 (メートル)。Godot のアドオン (sai-vr/scripts/avatar_skeleton_followers.gd) の
 * `maxf(0.1, motion_scale * 0.15)` に合わせた値。three-vrm には motion_scale に当たる値が
 * 無いので、0.1 に固定する。 */
export const FOLLOWER_BONE_MAX_REST_DISTANCE_M = 0.1;
/** 一つの armature を体の写しとして採るのに要る組の数の下限 (hips の組も必要)。体と関係の
 * ない小物の armature を、体の写しと取り違えないため。Godot のアドオンと同じ条件。 */
export const FOLLOWER_MIN_PAIRS = 3;

/** 体の humanoid の bone (raw bone) の一つ。 */
export interface FollowerSourceBone {
    /** VRM の humanoid の bone の名前 ('hips' 'leftUpperArm' など)。 */
    humanBone: string;
    /** 比べるための名前: glTF の node の元の名前から、末尾の `_数字` を落としたもの
     * (stripNameNumberSuffix)。 */
    name: string;
    /** rest の world 位置。 */
    position: Vec3;
}

/** 体とは別の armature の中にある node の一つ。 */
export interface FollowerCandidate {
    /** 呼び出し側が決める番号。親が先に来る順 (scene を traverse した順) に振る。 */
    id: number;
    /** この node が属する armature の根を表す番号。 */
    group: number;
    /** 比べるための名前 (FollowerSourceBone.name と同じ作り方)。 */
    name: string;
    /** rest の world 位置。 */
    position: Vec3;
}

export interface FollowerPair {
    candidateId: number;
    humanBone: string;
}

/** 体とは別の armature の bone を、体の humanoid の bone と組にする。
 * - 比べるための名前 (name) が体の bone と同じで、rest の world 位置が体の bone から
 *   FOLLOWER_BONE_MAX_REST_DISTANCE_M 以内の node を、その human bone の候補とする。
 * - 一つの armature の中で、ある human bone の候補がちょうど一つのときだけ組にする
 *   (二つ以上あるときは、どれを写すべきか決められないので組にしない)。
 * - hips の組があり、組が FOLLOWER_MIN_PAIRS 以上ある armature だけを採る。
 * 返す組は、candidateId の小さい順 (親が先) に並べる。 */
export function matchFollowerBones(
    sources: readonly FollowerSourceBone[],
    candidates: readonly FollowerCandidate[],
    maxDistance: number = FOLLOWER_BONE_MAX_REST_DISTANCE_M,
): FollowerPair[] {
    const byGroup = new Map<number, FollowerCandidate[]>();
    for (const c of candidates) {
        const list = byGroup.get(c.group);
        if (list) list.push(c);
        else byGroup.set(c.group, [c]);
    }
    const result: FollowerPair[] = [];
    for (const list of Array.from(byGroup.values())) {
        const pairs: FollowerPair[] = [];
        for (const source of sources) {
            const matches = list.filter(c => c.name === source.name
                && distance3(c.position, source.position) <= maxDistance);
            if (matches.length === 1) pairs.push({ candidateId: matches[0].id, humanBone: source.humanBone });
        }
        // 体の側に、比べるための名前が同じ bone が二つある VRM では、一つの node が二つの human bone の
        // 組になりうる。そのときは、sources の順で先に組になったほうだけを残す。
        const seen = new Set<number>();
        const unique = pairs.filter(p => (seen.has(p.candidateId) ? false : (seen.add(p.candidateId), true)));
        if (!unique.some(p => p.humanBone === 'hips') || unique.length < FOLLOWER_MIN_PAIRS) continue;
        result.push(...unique);
    }
    result.sort((a, b) => a.candidateId - b.candidateId);
    return result;
}

/** node の名前の末尾の `_数字` を落とす。VRM を書き出すソフトも、three の GLTFLoader も、
 * 同じ名前の node に `_2` `_3` … を付けて名前を重ならないようにするので、体の bone と、
 * 別の armature にある同じ役目の bone を、名前で比べるときに落とす。 */
export function stripNameNumberSuffix(name: string): string {
    return name.replace(/_[0-9]+$/, '');
}

function distance3(a: Vec3, b: Vec3): number {
    return Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
}

/** 呼吸程度のゆっくりした体の揺れ。正規化された背骨 (spine) に足す回転 (ラジアン)。
 * x は前後 (約 4 秒で一往復)、z は左右 (約 7 秒で一往復)。どちらもごく小さい。 */
export function breathingSway(timeSec: number): { x: number; z: number } {
    const tau = Math.PI * 2;
    return {
        x: 0.012 * Math.sin(timeSec * tau / 4.2),
        z: 0.008 * Math.sin(timeSec * tau / 7.3 + 0.6),
    };
}
