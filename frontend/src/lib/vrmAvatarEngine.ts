/** ルームビューの 3D アバターを描く (docs/intent/screen_avatar.md 設計 3)。
 *
 * ⚠ three と @pixiv/three-vrm を import するのは、このモジュールだけ。このモジュールは
 * components/VrmAvatar.tsx から動的 import (`import()`) でだけ読む。静的に import すると、
 * VRM を持たないペルソナしかいない画面にも three が載る (必ず守ること 9)。
 *
 * VRM は一体 25〜150MB あるので、読み込んだ VRM と、それを描く renderer・scene・camera・
 * canvas は、このモジュールの中に一組だけ持つ (GPU に置く VRM は同時に一体まで)。
 * ルームビューの部品 (借り手) は borrowVrmAvatar で借り、canvas を持つ借り手だけが、
 * 自分の枠の中に canvas を置く。どの借り手が canvas を持つかは chooseAvatarHolder
 * (lib/vrmAvatarMath.ts) で決める。
 * - 持ち主の URL の VRM を持つ。別の URL の借り手が持ち主になったら、古い VRM は捨てる。
 * - その URL の借り手が一人もいなくなっても、VRM_KEEP_AFTER_RELEASE_MS の間は持っておく。
 * - 描画のループは、持ち主が画面に見えていて、タブが表に出ている間だけ回す。 */
import * as THREE from 'three';
import { GLTFLoader, type GLTF } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { VRMHumanBoneList, VRMLoaderPlugin, VRMUtils, type VRM, type VRMHumanBoneName } from '@pixiv/three-vrm';
import {
    AVATAR_CAMERA_FOV_DEG,
    MOUTH_CLOSE_TIME_CONSTANT_SEC,
    MOUTH_OPEN_TIME_CONSTANT_SEC,
    VRM_KEEP_AFTER_RELEASE_MS,
    avatarPixelRatio,
    blinkWeight,
    BLINK_TOTAL_SEC,
    breathingSway,
    chooseAvatarHolder,
    computeAvatarFraming,
    decideMouthTarget,
    matchFollowerBones,
    nextBlinkDelaySec,
    restArmRotationZ,
    smoothToward,
    stripNameNumberSuffix,
    type AvatarBonePositions,
    type AvatarFramingMode,
    type FollowerCandidate,
    type FollowerSourceBone,
    type VrmAvatarFailure,
    type VrmAvatarStatus,
    type VrmAvatarView,
} from './vrmAvatarMath';
// three に依らない小さなモジュール。鳴っている声の音量を読む。
import { getVoiceLevel } from './voiceLevelMeter';

export interface VrmAvatarBorrowerState {
    /** canvas を置く要素。持ち主になったら、canvas をこの中へ移す。 */
    host: HTMLElement | null;
    /** この借り手の枠が画面に見えているか (IntersectionObserver の判定)。 */
    visible: boolean;
    framing: AvatarFramingMode;
    /** このペルソナがいま喋っているか。持ち主の値だけを口に使う。 */
    speaking: boolean;
    /** このペルソナの声が鳴っているか。持ち主の値だけを口に使う。鳴っていて音量が読めれば、
     * 口の開きを音量に合わせる。 */
    voiced: boolean;
}

export interface VrmAvatarLease {
    update(state: VrmAvatarBorrowerState): void;
    release(): void;
}

interface Borrower extends VrmAvatarBorrowerState {
    id: number;
    url: string;
    visibleSince: number;
    onChange: (view: VrmAvatarView) => void;
    lastView: VrmAvatarView | null;
}

/** 体とは別の armature (服や髪の armature) にある bone と、それが姿勢を写す体の raw bone の組。 */
interface FollowerBone {
    node: THREE.Object3D;
    source: THREE.Object3D;
    /** rest のときの `inverse(source.matrixWorld) × node.matrixWorld`。 */
    offset: THREE.Matrix4;
}

interface Entry {
    url: string;
    status: 'loading' | 'ready';
    progress: number | null;
    abort: AbortController;
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    canvas: HTMLCanvasElement;
    vrm: VRM | null;
    bones: AvatarBonePositions | null;
    /** 体とは別の armature にあって、体の姿勢を写す bone (buildFollowerBones)。親が先に来る順。 */
    followers: FollowerBone[];
    /** 前の一枚を描いた時刻 (requestAnimationFrame の時刻、ミリ秒)。ループが止まると null。 */
    lastFrameAt: number | null;
    raf: number | null;
    resizeObserver: ResizeObserver | null;
    observedHost: HTMLElement | null;
    framing: AvatarFramingMode | null;
    size: { width: number; height: number };
    onContextLost: (event: Event) => void;
    // 動きの状態
    time: number;
    mouth: number;
    blinkElapsed: number | null;
    blinkWait: number;
}

const borrowers = new Map<number, Borrower>();
let nextBorrowerId = 1;
let visibilityCounter = 0;
let holderId: number | null = null;
let entry: Entry | null = null;
let disposeTimer: ReturnType<typeof setTimeout> | null = null;
/** 読み込めなかった URL と理由。同じ URL は、ページを開き直すまで読み直さない
 * (喋るたびに 100MB を読み直して、毎回同じ失敗をしないため)。VRM を入れ直すと URL の
 * `?v=` が変わるので、新しいファイルは読みに行く。 */
const failures = new Map<string, { reason: VrmAvatarFailure; detail?: string }>();
/** WebGL が使えないと分かったら、どの URL も試さない。 */
let webglUnavailable: { detail?: string } | null = null;
let documentListenerInstalled = false;

export function borrowVrmAvatar(url: string, onChange: (view: VrmAvatarView) => void): VrmAvatarLease {
    installDocumentListener();
    const id = nextBorrowerId++;
    const borrower: Borrower = {
        id, url, onChange, lastView: null,
        host: null, visible: false, visibleSince: 0, framing: 'upper', speaking: false, voiced: false,
    };
    borrowers.set(id, borrower);
    recompute();
    return {
        update(state) {
            const b = borrowers.get(id);
            if (!b) return;
            if (state.visible && !b.visible) b.visibleSince = ++visibilityCounter;
            b.host = state.host;
            b.visible = state.visible;
            b.framing = state.framing;
            b.speaking = state.speaking;
            b.voiced = state.voiced;
            recompute();
        },
        release() {
            if (!borrowers.delete(id)) return;
            if (holderId === id) holderId = null;
            recompute();
        },
    };
}

function installDocumentListener() {
    if (documentListenerInstalled || typeof document === 'undefined') return;
    documentListenerInstalled = true;
    document.addEventListener('visibilitychange', () => updateLoop());
}

/** 借り手の変化のたびに呼ぶ。持ち主を決め直し、必要なら VRM を読み込み・捨て、canvas を
 * 持ち主の枠へ移し、描画のループを動かすか止めるかを決め、借り手へ様子を知らせる。 */
function recompute() {
    const list = Array.from(borrowers.values());
    holderId = chooseAvatarHolder(list, holderId);
    const holder = holderId !== null ? borrowers.get(holderId) ?? null : null;

    if (holder && holder.visible && !failures.has(holder.url) && !webglUnavailable) {
        if (entry && entry.url !== holder.url) {
            disposeEntry(entry);
            entry = null;
        }
        if (!entry) entry = createEntry(holder.url);
    }

    if (entry) {
        const e = entry;
        if (holder && holder.url === e.url && holder.host) {
            if (e.canvas.parentElement !== holder.host) holder.host.appendChild(e.canvas);
            observeHost(e, holder.host);
            if (e.framing !== holder.framing) {
                e.framing = holder.framing;
                updateCamera(e);
            }
        }
        // その URL の借り手が一人もいなければ、しばらく持ってから捨てる。
        const used = list.some(b => b.url === e.url);
        if (used) {
            if (disposeTimer) { clearTimeout(disposeTimer); disposeTimer = null; }
        } else if (!disposeTimer) {
            disposeTimer = setTimeout(() => {
                disposeTimer = null;
                if (entry === e && !Array.from(borrowers.values()).some(b => b.url === e.url)) {
                    disposeEntry(e);
                    entry = null;
                }
            }, VRM_KEEP_AFTER_RELEASE_MS);
        }
    }

    updateLoop();
    notifyAll();
}

function viewFor(b: Borrower): VrmAvatarView {
    const failure = webglUnavailable
        ? { reason: 'webgl' as const, detail: webglUnavailable.detail }
        : failures.get(b.url);
    if (failure) return { holding: false, status: { phase: 'failed', reason: failure.reason, detail: failure.detail } };
    if (b.id !== holderId || !entry || entry.url !== b.url) return { holding: false, status: { phase: 'idle' } };
    if (entry.status === 'loading') return { holding: false, status: { phase: 'loading', progress: entry.progress } };
    const attached = !!b.host && entry.canvas.parentElement === b.host;
    return { holding: attached, status: { phase: 'ready' } };
}

function sameView(a: VrmAvatarView | null, b: VrmAvatarView): boolean {
    if (!a || a.holding !== b.holding || a.status.phase !== b.status.phase) return false;
    if (a.status.phase === 'loading' && b.status.phase === 'loading') return a.status.progress === b.status.progress;
    if (a.status.phase === 'failed' && b.status.phase === 'failed') {
        return a.status.reason === b.status.reason && a.status.detail === b.status.detail;
    }
    return true;
}

function notifyAll() {
    for (const b of Array.from(borrowers.values())) {
        const view = viewFor(b);
        if (sameView(b.lastView, view)) continue;
        b.lastView = view;
        b.onChange(view);
    }
}

function createEntry(url: string): Entry | null {
    let renderer: THREE.WebGLRenderer;
    try {
        renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, premultipliedAlpha: true });
    } catch (err) {
        webglUnavailable = { detail: err instanceof Error ? err.message : undefined };
        return null;
    }
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(avatarPixelRatio(typeof window !== 'undefined' ? window.devicePixelRatio : 1));
    const canvas = renderer.domElement;
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.display = 'block';

    const scene = new THREE.Scene();
    // three-vrm の例と同じ明るさの平行光。MToon の影の色は VRM の側に入っている。
    const light = new THREE.DirectionalLight(0xffffff, Math.PI);
    light.position.set(1, 1, 1).normalize();
    scene.add(light);
    const camera = new THREE.PerspectiveCamera(AVATAR_CAMERA_FOV_DEG, 1, 0.05, 50);
    camera.position.set(0, 1.3, 3);
    scene.add(camera);

    const e: Entry = {
        url, status: 'loading', progress: null, abort: new AbortController(),
        renderer, scene, camera, canvas, vrm: null, bones: null, followers: [],
        lastFrameAt: null, raf: null, resizeObserver: null, observedHost: null,
        framing: null, size: { width: 0, height: 0 },
        onContextLost: () => {},
        time: 0, mouth: 0, blinkElapsed: null, blinkWait: nextBlinkDelaySec(Math.random()),
    };
    e.onContextLost = (event: Event) => {
        event.preventDefault();
        fail(e, 'contextLost');
    };
    canvas.addEventListener('webglcontextlost', e.onContextLost);
    void loadInto(e);
    return e;
}

async function loadInto(e: Entry) {
    let buffer: ArrayBuffer;
    try {
        const res = await fetch(e.url, { signal: e.abort.signal });
        if (!res.ok) {
            fail(e, 'http', String(res.status));
            return;
        }
        buffer = await readWithProgress(res, e);
    } catch (err) {
        if (e.abort.signal.aborted) return;
        fail(e, 'network', err instanceof Error ? err.message : undefined);
        return;
    }
    if (entry !== e) return;

    let vrm: VRM | undefined;
    let gltf: GLTF;
    try {
        const loader = new GLTFLoader();
        loader.register(parser => new VRMLoaderPlugin(parser));
        gltf = await loader.parseAsync(buffer, '');
        vrm = gltf.userData.vrm as VRM | undefined;
        if (!vrm) {
            VRMUtils.deepDispose(gltf.scene);
            fail(e, 'notVrm');
            return;
        }
        if (entry !== e) {
            VRMUtils.deepDispose(vrm.scene);
            return;
        }
        // three-vrm の例と同じ下ごしらえ: 使われていない頂点を外し、スケルトンをまとめ、
        // VRM 0.x を VRM 1.0 と同じ向き (正面が +Z) へ回す。
        VRMUtils.removeUnnecessaryVertices(gltf.scene);
        VRMUtils.combineSkeletons(gltf.scene);
        VRMUtils.rotateVRM0(vrm);
        vrm.scene.traverse(obj => { obj.frustumCulled = false; });
    } catch (err) {
        if (vrm) VRMUtils.deepDispose(vrm.scene);
        if (entry !== e) return;
        fail(e, 'parse', err instanceof Error ? err.message : undefined);
        return;
    }

    e.vrm = vrm;
    e.scene.add(vrm.scene);
    // 組は、まだ読み込んだままの rest の姿勢のうちに作る (applyRestPose より前)。
    e.followers = buildFollowerBones(vrm, gltf);
    applyRestPose(vrm);
    vrm.scene.updateMatrixWorld(true);
    e.bones = readBonePositions(vrm);
    if (vrm.lookAt) vrm.lookAt.target = e.camera;
    // 最初の一枚を描く前に、ばね (spring bone) を立ち姿に落ち着かせる。毎フレームと同じ順で
    // 呼ぶ (updateVrm)。そうしないと、最初の一枚だけ服の armature が rest の姿勢で出る。
    updateVrm(e, vrm, 0);
    e.status = 'ready';
    updateCamera(e);
    recompute();
}

async function readWithProgress(res: Response, e: Entry): Promise<ArrayBuffer> {
    const total = Number(res.headers.get('content-length')) || 0;
    if (!res.body) return res.arrayBuffer();
    const reader = res.body.getReader();
    const chunks: Uint8Array[] = [];
    let received = 0;
    let lastReported = -1;
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value) continue;
        chunks.push(value);
        received += value.byteLength;
        if (total > 0) {
            const percent = Math.min(99, Math.floor((received / total) * 100));
            if (percent !== lastReported && entry === e) {
                lastReported = percent;
                e.progress = percent;
                notifyAll();
            }
        }
    }
    const out = new Uint8Array(received);
    let offset = 0;
    for (const chunk of chunks) {
        out.set(chunk, offset);
        offset += chunk.byteLength;
    }
    return out.buffer;
}

function fail(e: Entry, reason: VrmAvatarFailure, detail?: string) {
    failures.set(e.url, { reason, detail });
    if (entry === e) {
        disposeEntry(e);
        entry = null;
    }
    recompute();
}

/** 読み込んだままの T ポーズから、両腕を下ろした立ち姿にする (正規化された humanoid)。 */
function applyRestPose(vrm: VRM) {
    const version = vrm.meta?.metaVersion;
    const leftUpperArm = vrm.humanoid.getNormalizedBoneNode('leftUpperArm');
    const rightUpperArm = vrm.humanoid.getNormalizedBoneNode('rightUpperArm');
    if (leftUpperArm) leftUpperArm.rotation.z = restArmRotationZ('left', version);
    if (rightUpperArm) rightUpperArm.rotation.z = restArmRotationZ('right', version);
}

function readBonePositions(vrm: VRM): AvatarBonePositions | null {
    const at = (name: Parameters<VRM['humanoid']['getRawBoneNode']>[0]) => {
        const node = vrm.humanoid.getRawBoneNode(name);
        if (!node) return null;
        const v = node.getWorldPosition(new THREE.Vector3());
        return { x: v.x, y: v.y, z: v.z };
    };
    const hips = at('hips');
    const head = at('head');
    if (!hips || !head) return null;
    return {
        hips, head,
        leftFoot: at('leftFoot'), rightFoot: at('rightFoot'),
        leftUpperArm: at('leftUpperArm'), rightUpperArm: at('rightUpperArm'),
    };
}

function observeHost(e: Entry, host: HTMLElement) {
    if (e.observedHost === host) return;
    e.resizeObserver?.disconnect();
    e.observedHost = host;
    const measure = () => {
        const width = Math.max(1, Math.round(host.clientWidth));
        const height = Math.max(1, Math.round(host.clientHeight));
        if (width === e.size.width && height === e.size.height) return;
        e.size = { width, height };
        e.renderer.setPixelRatio(avatarPixelRatio(window.devicePixelRatio));
        e.renderer.setSize(width, height, false);
        updateCamera(e);
    };
    if (typeof ResizeObserver !== 'undefined') {
        e.resizeObserver = new ResizeObserver(measure);
        e.resizeObserver.observe(host);
    }
    measure();
}

function updateCamera(e: Entry) {
    const { width, height } = e.size;
    if (width > 0 && height > 0) {
        e.camera.aspect = width / height;
    }
    if (e.bones) {
        const framing = computeAvatarFraming(e.bones, e.framing ?? 'upper', e.camera.aspect, AVATAR_CAMERA_FOV_DEG);
        e.camera.position.set(framing.position.x, framing.position.y, framing.position.z);
        e.camera.lookAt(framing.target.x, framing.target.y, framing.target.z);
    }
    e.camera.updateProjectionMatrix();
    // ループが止まっている間に大きさが変わっても、次に見えたときの最初の一枚が正しいように。
    if (e.status === 'ready' && e.raf === null) e.renderer.render(e.scene, e.camera);
}

function shouldRun(): boolean {
    if (!entry || entry.status !== 'ready') return false;
    const holder = holderId !== null ? borrowers.get(holderId) : undefined;
    if (!holder || !holder.visible || holder.url !== entry.url) return false;
    if (!holder.host || entry.canvas.parentElement !== holder.host) return false;
    return typeof document === 'undefined' || document.visibilityState === 'visible';
}

function updateLoop() {
    const e = entry;
    if (!e) return;
    if (shouldRun()) {
        if (e.raf !== null) return;
        e.lastFrameAt = null;
        const tick = (now: number) => {
            if (entry !== e || !shouldRun()) {
                e.raf = null;
                e.lastFrameAt = null;
                return;
            }
            e.raf = requestAnimationFrame(tick);
            // 止まっていた間の時間をまとめて進めない (ばねが暴れる)。
            const elapsed = e.lastFrameAt === null ? 0 : (now - e.lastFrameAt) / 1000;
            e.lastFrameAt = now;
            const dt = Math.min(Math.max(elapsed, 0), 1 / 20);
            animate(e, dt);
            e.renderer.render(e.scene, e.camera);
        };
        e.raf = requestAnimationFrame(tick);
    } else if (e.raf !== null) {
        cancelAnimationFrame(e.raf);
        e.raf = null;
        e.lastFrameAt = null;
    }
}

function animate(e: Entry, dt: number) {
    const vrm = e.vrm;
    if (!vrm) return;
    e.time += dt;
    const holder = holderId !== null ? borrowers.get(holderId) : undefined;
    const speaking = !!holder?.speaking;
    const voiced = !!holder?.voiced;

    // まばたき (不規則な間隔)
    if (e.blinkElapsed === null) {
        e.blinkWait -= dt;
        if (e.blinkWait <= 0) e.blinkElapsed = 0;
    }
    let blink = 0;
    if (e.blinkElapsed !== null) {
        e.blinkElapsed += dt;
        blink = blinkWeight(e.blinkElapsed);
        if (e.blinkElapsed >= BLINK_TOTAL_SEC) {
            e.blinkElapsed = null;
            e.blinkWait = nextBlinkDelaySec(Math.random());
        }
    }

    // 口: 声が鳴っていて音量が読めれば音量に合わせ、読めなければ (または文字だけの発話なら)
    // 時間で開け閉めし、どちらでもなければ閉じる (decideMouthTarget)。
    const mouthTarget = decideMouthTarget({
        voiced, speaking, voiceLevel: voiced ? getVoiceLevel() : null, timeSec: e.time,
    });
    e.mouth = smoothToward(
        e.mouth, mouthTarget, dt,
        mouthTarget > e.mouth ? MOUTH_OPEN_TIME_CONSTANT_SEC : MOUTH_CLOSE_TIME_CONSTANT_SEC,
    );

    const expressions = vrm.expressionManager;
    if (expressions) {
        expressions.setValue('blink', blink);
        expressions.setValue('aa', e.mouth < 0.01 ? 0 : e.mouth);
    }

    // 呼吸程度の揺れ
    const spine = vrm.humanoid.getNormalizedBoneNode('spine');
    if (spine) {
        const sway = breathingSway(e.time);
        spine.rotation.set(sway.x, 0, sway.z);
    }

    updateVrm(e, vrm, dt);
}

/** VRM を一フレーム分進める。
 *
 * three-vrm の `VRM.update(delta)` (node_modules/@pixiv/three-vrm/lib/three-vrm.module.js の
 * VRM クラス) は、親の `VRMCore.update` で humanoid の更新 (normalized bone の姿勢を raw bone へ
 * 写す)・lookAt・expression を進め、その後で node constraint、spring bone、material の順に
 * 進める。体とは別の armature の bone へ姿勢を写すのは、raw bone の姿勢が決まった後で、
 * spring bone より前でなければならない (写す bone の下に spring bone の鎖が付いている VRM で、
 * 鎖の根が動いた後に揺れを計算するため)。`VRM.update` の途中に処理を差し込む口は無いので、
 * 先に `humanoid.update()` を呼んで raw bone の姿勢を決め、写してから `VRM.update` を呼ぶ。
 * `VRM.update` の中の humanoid の更新は、同じ normalized bone から同じ値をもう一度写すだけになる。 */
function updateVrm(e: Entry, vrm: VRM, dt: number) {
    if (e.followers.length > 0) {
        vrm.humanoid.update();
        copyFollowerPose(e.followers);
    }
    vrm.update(dt);
}

/** 体の bone と、別の armature の bone を比べるための名前。glTF の node の元の名前から、
 * 末尾の `_数字` を落としたもの。
 *
 * 元の名前は、GLTFLoader が持つ対応 `parser.associations` (Object3D → `{ nodes: 番号 }`) から
 * `parser.json.nodes[番号].name` で取る。VRMUtils の下ごしらえ (removeUnnecessaryVertices・
 * combineSkeletons) は geometry と skeleton を作り直すが、bone の Object3D はそのまま使うので、
 * この対応は崩れない。取れない node は、読み込んだ後の名前を使う。
 *
 * 末尾の `_数字` を落とすのは、同じ名前の node を重ならないようにする付け足しが二か所で
 * 起きるため。一つは VRM を書き出すソフトで、実測した VRM (erys_skimmia) では、ファイルの中の
 * 名前がもう `Hips` (体) と `Hips_2` `Hips_3` (服の armature) になっていた。もう一つは
 * GLTFLoader で、読み込んだ後の名前に `_2` `_3` を付ける。Godot のアドオン
 * (avatar_skeleton_followers.gd) も、同じ `_数字` を落として比べている。 */
function comparableNodeName(gltf: GLTF, obj: THREE.Object3D): string {
    const index = gltf.parser.associations.get(obj)?.nodes;
    const nodes: unknown = gltf.parser.json?.nodes;
    if (index !== undefined && Array.isArray(nodes)) {
        const name: unknown = (nodes[index] as { name?: unknown } | undefined)?.name;
        if (typeof name === 'string' && name) return stripNameNumberSuffix(name);
    }
    return stripNameNumberSuffix(obj.name);
}

/** 体とは別の armature (服や髪の armature) の bone のうち、体の humanoid の bone と同じ名前で
 * rest の位置が近いものを探して組にし、rest のときの相対行列を覚える。Godot のアドオン
 * (sai-vr/scripts/avatar_skeleton_followers.gd) と同じ考え方。VRM の仕様 (VRMC_node_constraint
 * など) で服の追従を書いていない VRM でも、服が体の姿勢に付いてくるようにするため。
 * vrm.scene が rest の姿勢のときに呼ぶ。 */
function buildFollowerBones(vrm: VRM, gltf: GLTF): FollowerBone[] {
    const humanoid = vrm.humanoid;
    const rawToHuman = new Map<THREE.Object3D, VRMHumanBoneName>();
    for (const humanBone of VRMHumanBoneList) {
        const node = humanoid.getRawBoneNode(humanBone);
        if (node && !rawToHuman.has(node)) rawToHuman.set(node, humanBone);
    }
    const hips = humanoid.getRawBoneNode('hips');
    if (!hips) return [];
    vrm.scene.updateMatrixWorld(true);

    // 体の armature: hips の raw bone とその祖先。ここと raw bone に入る手前で、まとまりの根を決める。
    const bodyPath = new Set<THREE.Object3D>();
    for (let n: THREE.Object3D | null = hips; n; n = n.parent) bodyPath.add(n);
    const isBoundary = (n: THREE.Object3D) => n === vrm.scene || bodyPath.has(n) || rawToHuman.has(n);

    const position = (obj: THREE.Object3D) => {
        const v = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
        return { x: v.x, y: v.y, z: v.z };
    };
    const sources: FollowerSourceBone[] = [];
    for (const [node, humanBone] of Array.from(rawToHuman)) {
        sources.push({ humanBone, name: comparableNodeName(gltf, node), position: position(node) });
    }

    const candidates: FollowerCandidate[] = [];
    const candidateNodes: THREE.Object3D[] = [];
    const groupIds = new Map<THREE.Object3D, number>();
    const normalizedRoot = humanoid.normalizedHumanBonesRoot;
    // 親が先に来る順に番号を振る。normalized bone (VRMHumanoidRig の下) は見ない。
    const visit = (obj: THREE.Object3D) => {
        if (obj === normalizedRoot) return;
        if (obj !== vrm.scene && !rawToHuman.has(obj) && !(obj as THREE.Mesh).isMesh) {
            let root = obj;
            while (root.parent && !isBoundary(root.parent)) root = root.parent;
            let group = groupIds.get(root);
            if (group === undefined) {
                group = groupIds.size;
                groupIds.set(root, group);
            }
            candidates.push({ id: candidateNodes.length, group, name: comparableNodeName(gltf, obj), position: position(obj) });
            candidateNodes.push(obj);
        }
        for (const child of obj.children) visit(child);
    };
    visit(vrm.scene);

    const followers: FollowerBone[] = [];
    for (const pair of matchFollowerBones(sources, candidates)) {
        const node = candidateNodes[pair.candidateId];
        const source = humanoid.getRawBoneNode(pair.humanBone as VRMHumanBoneName);
        if (!node || !source) continue;
        const offset = new THREE.Matrix4().copy(source.matrixWorld).invert().multiply(node.matrixWorld);
        followers.push({ node, source, offset });
    }
    return followers;
}

const _followerWorld = new THREE.Matrix4();
const _followerParentInverse = new THREE.Matrix4();

/** 体の raw bone の今の姿勢を、rest のときの相対関係を保ったまま、組になった bone へ写す。
 * 写したい world 行列 `source.matrixWorld × offset` に、親の world 行列の逆行列を掛けて
 * local の行列にし、position・quaternion・scale へ分ける。体の bone と親の world 行列は、
 * 使う直前に祖先から計算し直す (間に写さない bone が挟まっていても、先に写した親の bone の
 * 姿勢が子に効くように)。 */
function copyFollowerPose(followers: readonly FollowerBone[]) {
    for (const f of followers) {
        f.source.updateWorldMatrix(true, false);
        _followerWorld.multiplyMatrices(f.source.matrixWorld, f.offset);
        const parent = f.node.parent;
        if (parent) {
            parent.updateWorldMatrix(true, false);
            _followerParentInverse.copy(parent.matrixWorld).invert();
            _followerWorld.premultiply(_followerParentInverse);
        }
        _followerWorld.decompose(f.node.position, f.node.quaternion, f.node.scale);
    }
}

function disposeEntry(e: Entry) {
    if (disposeTimer) { clearTimeout(disposeTimer); disposeTimer = null; }
    e.abort.abort();
    if (e.raf !== null) cancelAnimationFrame(e.raf);
    e.raf = null;
    e.resizeObserver?.disconnect();
    e.resizeObserver = null;
    e.observedHost = null;
    // forceContextLoss は webglcontextlost を起こすので、先に聞くのをやめる。
    e.canvas.removeEventListener('webglcontextlost', e.onContextLost);
    if (e.vrm) {
        e.scene.remove(e.vrm.scene);
        VRMUtils.deepDispose(e.vrm.scene);
        e.vrm = null;
    }
    e.renderer.dispose();
    e.renderer.forceContextLoss();
    e.canvas.remove();
}

export type { VrmAvatarStatus, VrmAvatarView };
