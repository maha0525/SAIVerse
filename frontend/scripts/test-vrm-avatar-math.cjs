// ルームビューの 3D アバターの決まり (lib/vrmAvatarMath.ts) を、実物の関数で確かめる。
// docs/intent/screen_avatar.md 設計 3。three・ブラウザ・サーバー・ペルソナ・LLM には触らない。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const file = path.join(__dirname, '../src/lib/vrmAvatarMath.ts');
const sourceText = fs.readFileSync(file, 'utf8');
// このモジュールは RoomView から静的に読まれる部品 (VrmAvatar) が使うので、three を読んではならない。
assert.ok(!/from\s+['"]three/.test(sourceText) && !/from\s+['"]@pixiv\//.test(sourceText),
    'vrmAvatarMath must not import three or @pixiv/three-vrm');
const compiled = ts.transpileModule(sourceText, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const mod = { exports: {} };
new Function('require', 'module', 'exports', compiled)(require, mod, mod.exports);
const {
    chooseAvatarHolder,
    avatarPixelRatio,
    computeAvatarFraming,
    restArmRotationZ,
    ARM_LOWER_ANGLE_RAD,
    mouthOpenness,
    smoothToward,
    nextBlinkDelaySec,
    blinkWeight,
    BLINK_TOTAL_SEC,
    breathingSway,
    VRM_KEEP_AFTER_RELEASE_MS,
    AVATAR_CAMERA_FOV_DEG,
    matchFollowerBones,
    stripNameNumberSuffix,
    FOLLOWER_BONE_MAX_REST_DISTANCE_M,
    FOLLOWER_MIN_PAIRS,
    VOICE_LEVEL_SILENT_RMS,
    VOICE_LEVEL_FULL_RMS,
    mouthOpennessFromVoiceLevel,
    decideMouthTarget,
    ORBIT_MIN_POLAR_RAD,
    ORBIT_MAX_POLAR_RAD,
    computeAvatarOrbitLimits,
    clampOrbitTarget,
    tapFromPress,
    isDoubleTap,
    TAP_MAX_DURATION_MS,
    TAP_MAX_MOVE_PX,
    DOUBLE_TAP_MAX_INTERVAL_MS,
    DOUBLE_TAP_MAX_DISTANCE_PX,
} = mod.exports;

// ── canvas を持つ借り手の決め方 ──
{
    const b = (id, visible, visibleSince) => ({ id, visible, visibleSince });
    // 誰も見えていない・誰もいない
    assert.equal(chooseAvatarHolder([], null), null);
    assert.equal(chooseAvatarHolder([b(1, false, 0)], null), null, 'nobody visible and no holder yet');
    // 見えている一人が持つ
    assert.equal(chooseAvatarHolder([b(1, true, 1)], null), 1);
    // 後から見えるようになったほうが持つ (右サイドバーの中と、ログに重ねるルームビュー)
    assert.equal(chooseAvatarHolder([b(1, true, 1), b(2, true, 2)], 1), 2, 'later-visible takes the canvas');
    assert.equal(chooseAvatarHolder([b(1, true, 3), b(2, true, 2)], 2), 1, 'sidebar opened after the overlay takes it back');
    // 見えなくなった借り手からは、見えているほうへ移る
    assert.equal(chooseAvatarHolder([b(1, true, 1), b(2, false, 2)], 2), 1);
    // 誰も見えていなければ、今の持ち主のまま (canvas を動かさない)
    assert.equal(chooseAvatarHolder([b(1, false, 1), b(2, false, 2)], 1), 1, 'keep the holder while nobody is visible');
    // 今の持ち主が返した後で、誰も見えていなければ持ち主なし
    assert.equal(chooseAvatarHolder([b(1, false, 1)], 2), null);
    // 同じ順番なら後から借りたほう
    assert.equal(chooseAvatarHolder([b(1, true, 5), b(2, true, 5)], null), 2);
    // 見えていない借り手が増えても、見えている持ち主は替わらない
    assert.equal(chooseAvatarHolder([b(1, true, 1), b(3, false, 0)], 1), 1);
}

assert.equal(VRM_KEEP_AFTER_RELEASE_MS, 180000, 'keep a released VRM for three minutes');

// ── 描画の解像度は devicePixelRatio の 2 まで ──
assert.equal(avatarPixelRatio(1), 1);
assert.equal(avatarPixelRatio(1.5), 1.5);
assert.equal(avatarPixelRatio(3), 2);
assert.equal(avatarPixelRatio(undefined), 1);
assert.equal(avatarPixelRatio(NaN), 1);
assert.equal(avatarPixelRatio(0), 1);

// ── 映す範囲 ──
{
    // 身長 1.6m ほどのモデル (正面 +Z)。
    const bones = {
        hips: { x: 0, y: 0.9, z: 0 },
        head: { x: 0, y: 1.4, z: 0 },
        leftFoot: { x: 0.1, y: 0.08, z: 0 },
        rightFoot: { x: -0.1, y: 0.08, z: 0 },
        leftUpperArm: { x: 0.17, y: 1.3, z: 0 },
        rightUpperArm: { x: -0.17, y: 1.3, z: 0 },
    };
    const tanHalf = Math.tan((AVATAR_CAMERA_FOV_DEG * Math.PI / 180) / 2);
    const visibleRangeY = (framing, aspect) => {
        const d = framing.position.z - framing.target.z;
        const half = d * tanHalf;
        return { bottom: framing.target.y - half, top: framing.target.y + half, halfWidth: half * aspect };
    };

    const upper = computeAvatarFraming(bones, 'upper', 3 / 4);
    const full = computeAvatarFraming(bones, 'full', 3 / 4);
    // カメラは正面 (+Z 側) から、まっすぐ向く
    for (const f of [upper, full]) {
        assert.ok(f.position.z > f.target.z, 'camera is in front of the model (+Z)');
        assert.equal(f.position.x, f.target.x);
        assert.equal(f.position.y, f.target.y);
    }
    const u = visibleRangeY(upper, 3 / 4);
    // 腰から上: 腰は映るが、足は映らない。頭のてっぺん (頭のボーンの少し上) まで映る。
    assert.ok(u.bottom < bones.hips.y && u.bottom > bones.leftFoot.y + 0.3, `upper bottom ${u.bottom}`);
    assert.ok(u.top > bones.head.y + 0.15, `upper top ${u.top}`);
    const fl = visibleRangeY(full, 3 / 4);
    // 全身: 足首より下まで映る
    assert.ok(fl.bottom < bones.leftFoot.y, `full bottom ${fl.bottom}`);
    assert.ok(fl.top > bones.head.y + 0.15);
    assert.ok(full.position.z > upper.position.z, 'full body needs a farther camera');
    // 横に細い枠では、体の幅が収まるようにカメラを引く
    const narrow = computeAvatarFraming(bones, 'upper', 0.3);
    assert.ok(narrow.position.z > upper.position.z, 'a narrow frame pulls the camera back');
    const n = visibleRangeY(narrow, 0.3);
    assert.ok(n.halfWidth >= 0.17 * 1.0, 'shoulders fit in a narrow frame');
    // 背の違うモデルでも、同じ割合で収まる (2 倍の大きさなら、カメラの距離も 2 倍)
    const scale = v => v && ({ x: v.x * 2, y: v.y * 2, z: v.z * 2 });
    const tall = Object.fromEntries(Object.entries(bones).map(([k, v]) => [k, scale(v)]));
    const tallFull = computeAvatarFraming(tall, 'full', 3 / 4);
    assert.ok(Math.abs((tallFull.position.z - tallFull.target.z) - 2 * (full.position.z - full.target.z)) < 1e-9);
    assert.ok(Math.abs(tallFull.target.y - 2 * full.target.y) < 1e-9);
    // 足や腕のボーンが無くても、数が壊れない
    const minimal = computeAvatarFraming({ hips: bones.hips, head: bones.head }, 'full', 1);
    assert.ok(Number.isFinite(minimal.position.z) && minimal.position.z > 0);
    // 縦横比が分からない (0 や NaN) ときも、数が壊れない
    for (const aspect of [0, NaN, -1, Infinity]) {
        const f = computeAvatarFraming(bones, 'upper', aspect);
        assert.ok(Number.isFinite(f.position.z), `aspect ${aspect}`);
    }
}

// ── 腕を下ろす回転 (正規化された上腕の Z 軸まわり) ──
{
    // VRM 1.0: 左手が +X にあるので、Z 軸まわりに負へ回すと下りる。右は逆。
    assert.equal(restArmRotationZ('left', '1'), -ARM_LOWER_ANGLE_RAD);
    assert.equal(restArmRotationZ('right', '1'), ARM_LOWER_ANGLE_RAD);
    // VRM 0.x: 正規化されたボーンの中では左手が -X にあるので、向きが逆になる。
    assert.equal(restArmRotationZ('left', '0'), ARM_LOWER_ANGLE_RAD);
    assert.equal(restArmRotationZ('right', '0'), -ARM_LOWER_ANGLE_RAD);
    // 版が分からないときは VRM 1.0 として扱う
    assert.equal(restArmRotationZ('left', undefined), -ARM_LOWER_ANGLE_RAD);
    // 実際に回したときに、腕の先が下を向くことを、回転の式で確かめる (+X の腕を z だけ回す)。
    const rotateZ = (x, y, angle) => ({ x: x * Math.cos(angle) - y * Math.sin(angle), y: x * Math.sin(angle) + y * Math.cos(angle) });
    assert.ok(rotateZ(1, 0, restArmRotationZ('left', '1')).y < -0.9, 'VRM 1.0 left arm (+X) goes down');
    assert.ok(rotateZ(-1, 0, restArmRotationZ('right', '1')).y < -0.9, 'VRM 1.0 right arm (-X) goes down');
    assert.ok(rotateZ(-1, 0, restArmRotationZ('left', '0')).y < -0.9, 'VRM 0.x left arm (-X) goes down');
    assert.ok(rotateZ(1, 0, restArmRotationZ('right', '0')).y < -0.9, 'VRM 0.x right arm (+X) goes down');
    // 真下までは下ろさない (腰回りの広い服にめり込まないように)
    assert.ok(ARM_LOWER_ANGLE_RAD < Math.PI / 2 && ARM_LOWER_ANGLE_RAD > Math.PI / 3);
}

// ── 喋っている間の口 ──
{
    const samples = [];
    for (let t = 0; t < 10; t += 1 / 60) samples.push(mouthOpenness(t));
    assert.ok(samples.every(v => v >= 0 && v <= 1), 'mouth openness stays in 0..1');
    // 閉じている時間と、大きく開く時間の両方がある
    assert.ok(samples.some(v => v === 0), 'the mouth closes between syllables');
    assert.ok(samples.some(v => v > 0.5), 'the mouth opens wide sometimes');
    // 開け閉めの回数は一秒に 3〜7 回ほど (閉じた状態から開いた回数を数える)
    let openings = 0;
    for (let i = 1; i < samples.length; i++) if (samples[i - 1] === 0 && samples[i] > 0) openings++;
    assert.ok(openings >= 30 && openings <= 70, `openings per 10 s: ${openings}`);
    // 開け閉めの間隔が一定ではない
    const starts = [];
    for (let i = 1; i < samples.length; i++) if (samples[i - 1] === 0 && samples[i] > 0) starts.push(i);
    const gaps = starts.slice(1).map((s, i) => s - starts[i]);
    assert.ok(Math.max(...gaps) - Math.min(...gaps) >= 3, 'syllable intervals vary');
    // 開き方の大きさも一定ではない
    const peaks = [];
    let peak = 0;
    for (const v of samples) {
        if (v > 0) peak = Math.max(peak, v);
        else if (peak > 0) { peaks.push(peak); peak = 0; }
    }
    assert.ok(Math.max(...peaks) - Math.min(...peaks) > 0.2, 'openness varies between syllables');
}

// ── 声の音量から口の開きへ ──
{
    assert.equal(VOICE_LEVEL_SILENT_RMS, 0.015);
    assert.equal(VOICE_LEVEL_FULL_RMS, 0.15);
    assert.equal(mouthOpennessFromVoiceLevel(0), 0);
    assert.equal(mouthOpennessFromVoiceLevel(0.015), 0, 'at the silent threshold the mouth is closed');
    assert.equal(mouthOpennessFromVoiceLevel(0.15), 1);
    assert.equal(mouthOpennessFromVoiceLevel(0.9), 1);
    const mid = mouthOpennessFromVoiceLevel((0.015 + 0.15) / 2);
    assert.ok(Math.abs(mid - 0.5) < 1e-9, `linear in between: ${mid}`);
    assert.ok(mouthOpennessFromVoiceLevel(0.05) < mouthOpennessFromVoiceLevel(0.1), 'louder opens wider');
    for (const bad of [NaN, -1, Infinity, -Infinity]) {
        const v = mouthOpennessFromVoiceLevel(bad);
        assert.ok(v >= 0 && v <= 1, `bad input ${bad} -> ${v}`);
    }
    assert.equal(mouthOpennessFromVoiceLevel(NaN), 0);
}

// ── 口の開きの目標の決め方 ──
{
    const t = 1.23;
    // 1. 声が鳴っていて音量が読めた → 音量から決める (speaking に関係なく)
    assert.equal(decideMouthTarget({ voiced: true, speaking: false, voiceLevel: 0.15, timeSec: t }), 1);
    assert.equal(decideMouthTarget({ voiced: true, speaking: true, voiceLevel: 0, timeSec: t }), 0,
        'a silent moment in the voice closes the mouth even while speaking');
    // 2. 声が鳴っているが音量が読めない → 時間で開け閉め
    assert.equal(decideMouthTarget({ voiced: true, speaking: false, voiceLevel: null, timeSec: t }), mouthOpenness(t));
    // 2. 文字だけの発話 → 時間で開け閉め (voiceLevel があっても voiced でなければ使わない)
    assert.equal(decideMouthTarget({ voiced: false, speaking: true, voiceLevel: null, timeSec: t }), mouthOpenness(t));
    assert.equal(decideMouthTarget({ voiced: false, speaking: true, voiceLevel: 0.15, timeSec: t }), mouthOpenness(t));
    // 3. どちらでもない → 閉じる
    assert.equal(decideMouthTarget({ voiced: false, speaking: false, voiceLevel: 0.15, timeSec: t }), 0);
    assert.equal(decideMouthTarget({ voiced: false, speaking: false, voiceLevel: null, timeSec: t }), 0);
    // 数でない音量は、読めなかったものとして扱う
    assert.equal(decideMouthTarget({ voiced: true, speaking: false, voiceLevel: NaN, timeSec: t }), mouthOpenness(t));
}

// ── 近づける速さ ──
assert.equal(smoothToward(0, 1, 0, 0.1), 0, 'no time, no change');
assert.equal(smoothToward(0.3, 0, 0.1, 0), 0, 'zero time constant jumps');
{
    const v = smoothToward(0, 1, 0.1, 0.1);
    assert.ok(v > 0.6 && v < 0.65, `one time constant ~ 63%: ${v}`);
    // 喋り終わったら閉じる: 0.5 秒でほぼ 0 まで
    let m = 0.8;
    for (let i = 0; i < 30; i++) m = smoothToward(m, 0, 1 / 60, 0.08);
    assert.ok(m < 0.01, `mouth closes after speaking: ${m}`);
}

// ── まばたき ──
{
    const delays = [];
    for (let r = 0; r < 1; r += 0.01) delays.push(nextBlinkDelaySec(r));
    assert.ok(delays.every(d => d > 0.2 && d <= 6), 'blink delays stay between 0.2 s and 6 s');
    assert.ok(delays.filter(d => d >= 2).length > 80, 'most blinks are 2-6 s apart');
    assert.ok(delays.some(d => d < 0.5), 'sometimes a quick double blink');
    assert.ok(new Set(delays.map(d => d.toFixed(2))).size > 50, 'delays are irregular');
    assert.ok(Number.isFinite(nextBlinkDelaySec(NaN)));
    assert.equal(blinkWeight(0), 0);
    assert.equal(blinkWeight(-1), 0);
    assert.equal(blinkWeight(BLINK_TOTAL_SEC + 0.01), 0, 'eyes open again after the blink');
    const weights = [];
    for (let t = 0; t <= BLINK_TOTAL_SEC; t += 0.005) weights.push(blinkWeight(t));
    assert.ok(weights.some(w => w === 1), 'eyes fully close');
    assert.ok(weights.every(w => w >= 0 && w <= 1));
    assert.ok(BLINK_TOTAL_SEC > 0.1 && BLINK_TOTAL_SEC < 0.3, 'a blink takes about 0.1-0.3 s');
}

// ── 体とは別の armature の bone を、体の humanoid の bone と組にする ──
{
    assert.equal(FOLLOWER_BONE_MAX_REST_DISTANCE_M, 0.1);
    assert.equal(FOLLOWER_MIN_PAIRS, 3);
    const p = (x, y, z = 0) => ({ x, y, z });
    const sources = [
        { humanBone: 'hips', name: 'Hips', position: p(0, 0.9) },
        { humanBone: 'spine', name: 'Spine', position: p(0, 1.0) },
        { humanBone: 'chest', name: 'Chest', position: p(0, 1.15) },
        { humanBone: 'leftUpperArm', name: 'upper_armL', position: p(0.17, 1.3) },
        { humanBone: 'rightUpperArm', name: 'upper_armR', position: p(-0.17, 1.3) },
    ];
    const c = (id, group, name, position) => ({ id, group, name, position });

    // 名前と位置が合えば組になり、親が先 (id の小さい順) に並ぶ
    const dress = [
        c(12, 1, 'upper_armL', p(0.17, 1.31)),
        c(10, 1, 'Hips', p(0, 0.92)),
        c(11, 1, 'Spine', p(0, 1.0)),
    ];
    assert.deepEqual(matchFollowerBones(sources, dress), [
        { candidateId: 10, humanBone: 'hips' },
        { candidateId: 11, humanBone: 'spine' },
        { candidateId: 12, humanBone: 'leftUpperArm' },
    ]);

    // 位置が遠い bone は組にしない (残りが 2 つになるので、この armature ごと捨てる)
    const far = [
        c(10, 1, 'Hips', p(0, 0.9)),
        c(11, 1, 'Spine', p(0, 1.0)),
        c(12, 1, 'upper_armL', p(0.17, 1.45)),
    ];
    assert.deepEqual(matchFollowerBones(sources, far), [], 'a far bone is not paired');
    const farButEnough = [...far, c(13, 1, 'Chest', p(0, 1.16))];
    assert.deepEqual(matchFollowerBones(sources, farButEnough).map(x => x.humanBone), ['hips', 'spine', 'chest'],
        'only the far bone is left out');

    // 同じ armature に同じ名前の候補が二つあれば、その human bone は組にしない
    const twin = [
        c(10, 1, 'Hips', p(0, 0.9)),
        c(11, 1, 'Spine', p(0, 1.0)),
        c(12, 1, 'Chest', p(0, 1.15)),
        c(13, 1, 'upper_armL', p(0.17, 1.3)),
        c(14, 1, 'upper_armL', p(0.18, 1.3)),
    ];
    assert.deepEqual(matchFollowerBones(sources, twin).map(x => x.humanBone), ['hips', 'spine', 'chest'],
        'two candidates for one human bone are ambiguous');

    // hips の組が無い armature は捨てる
    const noHips = [
        c(10, 1, 'Spine', p(0, 1.0)),
        c(11, 1, 'Chest', p(0, 1.15)),
        c(12, 1, 'upper_armL', p(0.17, 1.3)),
        c(13, 1, 'upper_armR', p(-0.17, 1.3)),
    ];
    assert.deepEqual(matchFollowerBones(sources, noHips), [], 'an armature without hips is dropped');

    // 組が 2 つ以下の armature は捨てる
    const tooFew = [c(10, 1, 'Hips', p(0, 0.9)), c(11, 1, 'Spine', p(0, 1.0))];
    assert.deepEqual(matchFollowerBones(sources, tooFew), [], 'two pairs are not enough');

    // armature ごとに判定する: 別の armature にある同じ名前の候補は、曖昧にならない
    const twoArmatures = [
        c(10, 1, 'Hips', p(0, 0.9)), c(11, 1, 'Spine', p(0, 1.0)), c(12, 1, 'Chest', p(0, 1.15)),
        c(20, 2, 'Hips', p(0, 0.9)), c(21, 2, 'Spine', p(0, 1.0)), c(22, 2, 'Chest', p(0, 1.15)),
        c(30, 3, 'Hips', p(0, 0.9)), c(31, 3, 'Spine', p(0, 1.0)),
    ];
    assert.deepEqual(matchFollowerBones(sources, twoArmatures).map(x => x.candidateId), [10, 11, 12, 20, 21, 22]);

    // 候補が無ければ何もしない
    assert.deepEqual(matchFollowerBones(sources, []), []);

    // 名前の末尾の `_数字` を落とす
    assert.equal(stripNameNumberSuffix('Hips_3'), 'Hips');
    assert.equal(stripNameNumberSuffix('lower_armL_2'), 'lower_armL');
    assert.equal(stripNameNumberSuffix('upper_armL'), 'upper_armL');
}

// ── 体の幅でカメラを引いたときの、余った縦の範囲の回し方 ──
{
    const bones = {
        hips: { x: 0, y: 0.9, z: 0 },
        head: { x: 0, y: 1.4, z: 0 },
        leftFoot: { x: 0.1, y: 0.08, z: 0 },
        rightFoot: { x: -0.1, y: 0.08, z: 0 },
        leftUpperArm: { x: 0.17, y: 1.3, z: 0 },
        rightUpperArm: { x: -0.17, y: 1.3, z: 0 },
    };
    const tanHalf = Math.tan((AVATAR_CAMERA_FOV_DEG * Math.PI / 180) / 2);
    const range = (f) => {
        const half = (f.position.z - f.target.z) * tanHalf;
        return { bottom: f.target.y - half, top: f.target.y + half };
    };
    // 'upper': 細い枠でも横長の枠でも、映る上端 (頭の上の余白) は同じで、下が伸びる
    const wide = range(computeAvatarFraming(bones, 'upper', 4 / 3));
    const tall = range(computeAvatarFraming(bones, 'upper', 0.45));
    assert.ok(Math.abs(wide.top - tall.top) < 1e-9, `upper keeps the head margin: ${wide.top} ${tall.top}`);
    assert.ok(tall.bottom < wide.bottom - 0.1, 'upper in a tall frame shows more of the body below');
    // 'full': 映る下端 (足先の下の余白) は同じで、上が伸びる
    const fullWide = range(computeAvatarFraming(bones, 'full', 1));
    const fullThin = range(computeAvatarFraming(bones, 'full', 0.2));
    assert.ok(Math.abs(fullWide.bottom - fullThin.bottom) < 1e-9, 'full keeps the feet near the bottom edge');
    assert.ok(fullWide.bottom < bones.leftFoot.y);
    assert.ok(fullThin.top > fullWide.top);
    // 'full': UI区画の枠 (幅 3・高さ 4) では、足首が枠の下端から 12%〜25% の高さに来る。
    // 枠の下寄りに重ねて出す名前の札 (下端から 6% の所から上へ一行) に足が隠れない位置。
    const column = range(computeAvatarFraming(bones, 'full', 3 / 4));
    const ankleFromBottom = (bones.leftFoot.y - column.bottom) / (column.top - column.bottom);
    assert.ok(ankleFromBottom > 0.12 && ankleFromBottom < 0.25, `ankle at ${ankleFromBottom} of the frame height`);
}

// ── カメラを回す・寄せる範囲 (docs/intent/screen_avatar.md「カメラを回す・寄せる」) ──
{
    const bones = {
        hips: { x: 0, y: 0.9, z: 0 },
        head: { x: 0, y: 1.4, z: 0 },
        leftFoot: { x: 0.1, y: 0.08, z: 0 },
        rightFoot: { x: -0.1, y: 0.08, z: 0 },
        leftUpperArm: { x: 0.17, y: 1.3, z: 0 },
        rightUpperArm: { x: -0.17, y: 1.3, z: 0 },
    };
    const deg = r => r * 180 / Math.PI;
    // 縦は、水平から 60 度上 (polar 30 度) から、水平から 10 度下 (polar 100 度) まで
    assert.ok(Math.abs(deg(ORBIT_MIN_POLAR_RAD) - 30) < 1e-9);
    assert.ok(Math.abs(deg(ORBIT_MAX_POLAR_RAD) - 100) < 1e-9);
    // 最初の構図のカメラは水平 (polar 90 度) なので、範囲の中にある
    assert.ok(ORBIT_MIN_POLAR_RAD < Math.PI / 2 && Math.PI / 2 < ORBIT_MAX_POLAR_RAD);

    const tanHalf = Math.tan((AVATAR_CAMERA_FOV_DEG * Math.PI / 180) / 2);
    const dist = f => Math.hypot(f.position.x - f.target.x, f.position.y - f.target.y, f.position.z - f.target.z);
    for (const aspect of [3 / 4, 4 / 3, 0.45, 1]) {
        const upper = computeAvatarFraming(bones, 'upper', aspect);
        const full = computeAvatarFraming(bones, 'full', aspect);
        const lu = computeAvatarOrbitLimits(bones, upper, aspect);
        const lf = computeAvatarOrbitLimits(bones, full, aspect);
        // 'upper' と 'full' で同じ範囲
        assert.deepEqual(lu, lf, `same limits for both framings at aspect ${aspect}`);
        // 最初の構図の距離は、上下限の中にある (戻したときに押し戻されない)
        for (const f of [upper, full]) {
            assert.ok(lu.minDistance <= dist(f) && dist(f) <= lu.maxDistance, `default distance within limits (${aspect})`);
            const c = clampOrbitTarget(f.target, lu.targetBounds);
            assert.deepEqual(c, f.target, `default target stays inside (${aspect})`);
        }
        // いちばん寄ったとき: 頭のボーンから頭のてっぺんまで (0.225 m) が、縦にも横にも収まり、
        // 縦か横のどちらかは、それを 1.25 倍した長さ (顎の下が少し入るくらい) ぴったり
        const visibleH = 2 * lu.minDistance * tanHalf;
        const visibleW = visibleH * aspect;
        const headSpan = 0.5 * 0.45;
        assert.ok(Math.min(visibleH, visibleW) >= headSpan, `face fits at the closest (${aspect})`);
        assert.ok(Math.abs(Math.min(visibleH, visibleW) - headSpan * 1.25) < 1e-9, `face fills the frame (${aspect})`);
        // いちばん引いたとき: 'full' の構図より 15% 遠い (全身に少し余裕がある)
        assert.ok(Math.abs(lu.maxDistance - dist(full) * 1.15) < 1e-9, `farthest is a bit beyond full (${aspect})`);
        assert.ok(lu.minDistance < dist(upper) && dist(full) < lu.maxDistance);
    }

    // 回る中心の範囲: 足の裏から頭のてっぺんまで、肩幅の内側 (体の縦の軸から 0.17 m)
    const limits = computeAvatarOrbitLimits(bones, computeAvatarFraming(bones, 'upper', 3 / 4), 3 / 4);
    const b = limits.targetBounds;
    assert.equal(b.axisX, 0);
    assert.equal(b.axisZ, 0);
    assert.ok(Math.abs(b.radius - 0.17) < 1e-9, `radius ${b.radius}`);
    assert.ok(Math.abs(b.maxY - (1.4 + 0.5 * 0.45)) < 1e-9, `top ${b.maxY}`);
    assert.ok(b.minY < bones.leftFoot.y && b.minY > bones.leftFoot.y - 0.15, `sole ${b.minY}`);
    // 押し戻し: 中なら同じ、上下は端へ、水平は軸からの向きを保って半径の上へ
    assert.deepEqual(clampOrbitTarget({ x: 0.1, y: 1.0, z: -0.05 }, b), { x: 0.1, y: 1.0, z: -0.05 });
    assert.equal(clampOrbitTarget({ x: 0, y: 5, z: 0 }, b).y, b.maxY);
    assert.equal(clampOrbitTarget({ x: 0, y: -3, z: 0 }, b).y, b.minY);
    const side = clampOrbitTarget({ x: 0.6, y: 1.0, z: 0.8 }, b);
    assert.ok(Math.abs(Math.hypot(side.x, side.z) - 0.17) < 1e-9, 'pushed onto the radius');
    assert.ok(Math.abs(side.x / side.z - 0.6 / 0.8) < 1e-9, 'direction from the axis is kept');
    // 背の違うモデルでも、同じ割合 (2 倍の大きさなら距離も範囲も 2 倍)
    const scale = v => v && ({ x: v.x * 2, y: v.y * 2, z: v.z * 2 });
    const tall = Object.fromEntries(Object.entries(bones).map(([k, v]) => [k, scale(v)]));
    const tallLimits = computeAvatarOrbitLimits(tall, computeAvatarFraming(tall, 'upper', 3 / 4), 3 / 4);
    assert.ok(Math.abs(tallLimits.minDistance - 2 * limits.minDistance) < 1e-9);
    assert.ok(Math.abs(tallLimits.maxDistance - 2 * limits.maxDistance) < 1e-9);
    assert.ok(Math.abs(tallLimits.targetBounds.radius - 2 * b.radius) < 1e-9);
    // ごく細い枠で最初の構図の中心が体の範囲の外に出るときは、範囲の側を広げて中心を入れる
    for (const aspect of [0.1, 0.05]) {
        for (const mode of ['upper', 'full']) {
            const f = computeAvatarFraming(bones, mode, aspect);
            const l = computeAvatarOrbitLimits(bones, f, aspect);
            assert.deepEqual(clampOrbitTarget(f.target, l.targetBounds), f.target, `thin frame ${mode} ${aspect}`);
            assert.ok(l.minDistance <= dist(f) && dist(f) <= l.maxDistance);
        }
    }
    // 足や腕のボーンが無くても、縦横比が分からなくても、数が壊れない
    for (const aspect of [0, NaN, -1, Infinity, 1]) {
        const minimal = { hips: bones.hips, head: bones.head };
        const l = computeAvatarOrbitLimits(minimal, computeAvatarFraming(minimal, 'full', aspect), aspect);
        for (const v of [l.minDistance, l.maxDistance, l.targetBounds.radius, l.targetBounds.minY, l.targetBounds.maxY]) {
            assert.ok(Number.isFinite(v), `finite with aspect ${aspect}`);
        }
        assert.ok(l.minDistance > 0 && l.minDistance < l.maxDistance);
    }
}

// ── ダブルタップの判定 ──
{
    assert.equal(TAP_MAX_DURATION_MS, 250);
    assert.equal(TAP_MAX_MOVE_PX, 10);
    assert.equal(DOUBLE_TAP_MAX_INTERVAL_MS, 300);
    assert.equal(DOUBLE_TAP_MAX_DISTANCE_PX, 30);
    const s = (timeMs, x, y) => ({ timeMs, x, y });
    // 短く、ほとんど動かない押し方はタップ
    const first = tapFromPress(s(1000, 100, 100), s(1080, 103, 101));
    assert.deepEqual(first, { downMs: 1000, upMs: 1080, x: 100, y: 100 });
    // 長押しと、ドラッグはタップではない
    assert.equal(tapFromPress(s(1000, 100, 100), s(1300, 100, 100)), null, 'a long press is not a tap');
    assert.equal(tapFromPress(s(1000, 100, 100), s(1050, 120, 100)), null, 'a drag is not a tap');
    assert.equal(tapFromPress(s(1000, 100, 100), s(990, 100, 100)), null, 'time going backwards is not a tap');
    // 近い所への、すぐ続く二回目のタップはダブルタップ
    const second = tapFromPress(s(1250, 110, 108), s(1320, 110, 108));
    assert.ok(isDoubleTap(first, second));
    // 一つ前のタップが無い・間が空きすぎ・離れすぎはダブルタップではない
    assert.equal(isDoubleTap(null, second), false);
    const late = tapFromPress(s(1400, 100, 100), s(1450, 100, 100));
    assert.equal(isDoubleTap(first, late), false, 'too slow');
    const far = tapFromPress(s(1200, 160, 100), s(1250, 160, 100));
    assert.equal(isDoubleTap(first, far), false, 'too far apart');
    // 二回目が一回目より前に始まっている (時刻が戻った) ものは数えない
    const overlapped = { downMs: 1050, upMs: 1100, x: 100, y: 100 };
    assert.equal(isDoubleTap(first, overlapped), false);
}

// ── 呼吸の揺れはごく小さい ──
for (let t = 0; t < 20; t += 0.1) {
    const s = breathingSway(t);
    assert.ok(Math.abs(s.x) <= 0.02 && Math.abs(s.z) <= 0.02);
}

console.log('VRM avatar holder choice, framing, camera orbit limits, double tap, arm pose, follower bones, mouth, blink and sway rules passed.');
