/**
 * 右サイドバーを出せない幅での、ログとルームビューの切り替えの決まり
 * (docs/intent/screen_avatar.md 設計 1 の「まはーが確定した三点」、必ず守ること 8)。
 *
 * React に依らない純粋な関数で、状態と出来事を受けて次の状態を返す。
 * 待ち時間のタイマーは呼び出し側 (useRoomViewAutoSwitch) が持ち、
 * `pendingReturnMs` が null でない間だけ動かして、時間が来たら
 * `{ type: 'returnTimerFired', token: pendingToken }` を渡す。
 *
 * 「発話」は speaking が true になってから、false になって待ち時間が過ぎるまで。
 * 待っている間にまた speaking が true になったら、同じ発話の続きとして扱う。
 *
 * 自動で切り替えるかは、グローバル設定 (SAIVERSE_ROOM_VIEW_AUTO_SWITCH、既定 OFF) で決まり、
 * `'signal'` の `autoSwitch` で渡される。OFF の間は、喋り出しても自動で 'room' にせず、
 * 「喋っています」の印も出さない。ヘッダーのボタンでの切り替え ('manual') と、発話の追跡
 * (speaking・待ち時間・isUtteranceActive) は ON と変わらない。発話の途中で ON → OFF に
 * なったら、自動で 'room' にしていた場合 ('auto') だけその場で 'log' に戻す ('manual' は
 * そのまま)。OFF → ON になった時点で続いている発話では自動で切り替えず、次の発話から効く
 * (overlayMode になった瞬間と同じ考え方)。
 */

export type RoomViewDisplay = 'log' | 'room';

/** 発話の間に読み上げの音声が鳴っていたとき、発話が終わってからログへ戻すまでの待ち時間。 */
export const RETURN_DELAY_AFTER_AUDIO_MS = 400;
/** 発話の間に読み上げの音声が鳴っていなかったとき (文字だけ) の待ち時間。 */
export const RETURN_DELAY_AFTER_TEXT_MS = 1500;

export interface RoomViewAutoSwitchState {
    /** overlayMode か。false の間は常に 'log' で、何も切り替えない。 */
    enabled: boolean;
    /** グローバル設定で、喋っている間に自動で 'room' にするか。最後の 'signal' の値。 */
    autoSwitch: boolean;
    view: RoomViewDisplay;
    /** 'room' になった経路。'auto' は自動か「喋っています」の印 (発話が終われば戻す)、
     * 'manual' はヘッダーのボタン (もう一度押すまで戻さない)。'log' の間は null。 */
    roomBy: 'auto' | 'manual' | null;
    speaking: boolean;
    /** この発話の間に、読み上げの音声が鳴ったか。 */
    audioInUtterance: boolean;
    /** この発話の間は自動で 'room' にしない印。利用者がボタンで 'log' に戻したとき、
     * または発話の途中で autoSwitch が OFF → ON になったときに立つ。 */
    logChosenInUtterance: boolean;
    /** 発話が終わって待っている間の待ち時間。待っていなければ null。 */
    pendingReturnMs: number | null;
    /** 待ちを始めるたびに増える番号。古いタイマーの知らせを捨てるのに使う。 */
    pendingToken: number;
}

export type RoomViewAutoSwitchEvent =
    | {
        type: 'signal';
        enabled: boolean;
        /** グローバル設定で、喋っている間に自動で 'room' にするか。 */
        autoSwitch: boolean;
        speaking: boolean;
        audioPlaying: boolean;
        /** 利用者が入力している最中か、過去のログを遡って読んでいる最中。 */
        userBusy: boolean;
    }
    /** ヘッダーの切り替えのボタン */
    | { type: 'toggle' }
    /** 「喋っています」の印 */
    | { type: 'pressIndicator' }
    | { type: 'returnTimerFired'; token: number }
    /** 見ている部屋 (currentBuildingId) が変わった */
    | { type: 'reset' };

export function initialRoomViewAutoSwitchState(): RoomViewAutoSwitchState {
    return {
        enabled: false,
        autoSwitch: false,
        view: 'log',
        roomBy: null,
        speaking: false,
        audioInUtterance: false,
        logChosenInUtterance: false,
        pendingReturnMs: null,
        pendingToken: 0,
    };
}

/** 発話の記録を捨てて 'log' に戻した状態。speaking はいまの値のまま持つ
 * (持たないと、次の知らせで false→true を誤って読む)。autoSwitch は設定の値なので持ち越す。 */
function backToLog(state: RoomViewAutoSwitchState, enabled: boolean, speaking: boolean): RoomViewAutoSwitchState {
    return {
        ...initialRoomViewAutoSwitchState(),
        enabled,
        autoSwitch: state.autoSwitch,
        speaking,
        pendingToken: state.pendingToken + 1,
    };
}

export function nextRoomViewAutoSwitchState(
    state: RoomViewAutoSwitchState,
    event: RoomViewAutoSwitchEvent,
): RoomViewAutoSwitchState {
    switch (event.type) {
        case 'signal': {
            // 設定 (autoSwitch) が変わったら、先に状態へ写す。
            let current = state;
            if (state.autoSwitch !== event.autoSwitch) {
                current = { ...state, autoSwitch: event.autoSwitch };
                if (!event.autoSwitch) {
                    // ON → OFF: 自動で 'room' にしていたら、その場で 'log' に戻す (発話の記録は残す)。
                    // 'manual' はそのまま。
                    if (current.view === 'room' && current.roomBy === 'auto') {
                        current = { ...current, view: 'log', roomBy: null };
                    }
                } else if (isUtteranceActive(current)) {
                    // OFF → ON: いま続いている発話では自動で切り替えない (次の発話から効く)。
                    current = { ...current, logChosenInUtterance: true };
                }
            }

            // 決まり 5: overlayMode でなくなったら 'log' に戻す。
            if (!event.enabled) {
                if (!current.enabled && current.speaking === event.speaking) return current;
                return backToLog(current, false, event.speaking);
            }
            // overlayMode になった瞬間に続いている発話では、自動で切り替えない (印は出す)。
            if (!current.enabled) {
                return {
                    ...backToLog(current, true, event.speaking),
                    audioInUtterance: event.speaking && event.audioPlaying,
                };
            }

            if (!current.speaking && event.speaking) {
                // 待っている間に speaking が戻った = 同じ発話の続き。戻すのをやめる (決まり 3)。
                const continuing = current.pendingReturnMs !== null;
                let next: RoomViewAutoSwitchState = {
                    ...current,
                    speaking: true,
                    pendingReturnMs: null,
                    audioInUtterance: (continuing && current.audioInUtterance) || event.audioPlaying,
                    logChosenInUtterance: continuing && current.logChosenInUtterance,
                };
                // 決まり 1: 設定が OFF・入力中・遡って読んでいる最中・この発話でログを選び直した後は、切り替えない。
                if (event.autoSwitch && next.view === 'log' && !event.userBusy && !next.logChosenInUtterance) {
                    next = { ...next, view: 'room', roomBy: 'auto' };
                }
                return next;
            }

            if (current.speaking && !event.speaking) {
                // 決まり 3: 少し待ってから戻す。待つ長さは、この発話の間に音声が鳴ったかで決める。
                const audio = current.audioInUtterance || event.audioPlaying;
                return {
                    ...current,
                    speaking: false,
                    audioInUtterance: audio,
                    pendingReturnMs: audio ? RETURN_DELAY_AFTER_AUDIO_MS : RETURN_DELAY_AFTER_TEXT_MS,
                    pendingToken: current.pendingToken + 1,
                };
            }

            if (current.speaking && event.audioPlaying && !current.audioInUtterance) {
                return { ...current, audioInUtterance: true };
            }
            return current;
        }

        case 'returnTimerFired': {
            if (event.token !== state.pendingToken || state.pendingReturnMs === null) return state;
            const ended: RoomViewAutoSwitchState = {
                ...state,
                pendingReturnMs: null,
                audioInUtterance: false,
                logChosenInUtterance: false,
            };
            // ボタンで 'room' にした場合 ('manual') は戻さない (決まり 4)。
            if (ended.view === 'room' && ended.roomBy === 'auto') {
                return { ...ended, view: 'log', roomBy: null };
            }
            return ended;
        }

        case 'toggle': {
            // 決まり 4
            if (!state.enabled) return state;
            if (state.view === 'log') {
                return { ...state, view: 'room', roomBy: 'manual' };
            }
            const inUtterance = state.speaking || state.pendingReturnMs !== null;
            return {
                ...state,
                view: 'log',
                roomBy: null,
                logChosenInUtterance: state.logChosenInUtterance || inUtterance,
            };
        }

        case 'pressIndicator': {
            // 決まり 2: 印で 'room' にした場合も、発話が終われば戻す。
            if (!showSpeakingIndicator(state)) return state;
            return { ...state, view: 'room', roomBy: 'auto' };
        }

        case 'reset': {
            // 決まり 5: 見ている部屋が変わったら 'log' に戻す。
            if (state.view === 'log' && state.pendingReturnMs === null && !state.logChosenInUtterance && !state.audioInUtterance) {
                return state;
            }
            return backToLog(state, state.enabled, state.speaking);
        }
    }
}

/** ルームビューに出すペルソナ (docs/intent/screen_avatar.md 設計 2)。文字が流れている
 * 吹き出しの持ち主、読み上げの音声が鳴っている吹き出しの持ち主、末尾の assistant の
 * 吹き出しの持ち主の順に選ぶ。字幕 (page.tsx の roomViewCaptionContent) と同じ順なので、
 * 鳴っている声・字幕と、映る姿が別のペルソナにならない。 */
export function resolveRoomViewSpeakerId(
    streamingSpeakerId: string | undefined,
    audioSpeakerId: string | undefined,
    lastSpeakerId: string | undefined,
): string | undefined {
    return streamingSpeakerId ?? audioSpeakerId ?? lastSpeakerId;
}

/** 鳴っている声の持ち主のペルソナ (docs/intent/screen_avatar.md 「口を声の音量に合わせる」)。
 *
 * 声の messageId と同じ id の吹き出しがあれば、その持ち主。無ければ、文字が流れている途中で
 * まだ id を持たない吹き出しの持ち主。
 *
 * 流し込みの途中の吹き出しは、発言が保存された合図 (page.tsx の `speak_persisted`) が届くまで
 * id を持たない。音声のアドオン (saiverse-voice-tts) は、その前から、同じ発言の声を一切れずつ
 * 鳴らし始める (audio_ready の message_id は、保存される行の id)。id が同じ吹き出しだけを
 * 探すと、文字が流れ終わるまで声の持ち主が見つからず、その間は口が音量に合わない。
 *
 * @param matchedBubble 声の messageId と同じ id の吹き出し。無ければ undefined。
 *   speakerId は、その吹き出しの持ち主 (分からなければ undefined)。
 * @param unsavedStreamingSpeakerId 文字が流れている途中で、まだ id を持たない吹き出しの持ち主。
 *   そういう吹き出しが無ければ undefined。 */
export function resolveRoomViewVoicedId(
    matchedBubble: { speakerId: string | undefined } | undefined,
    unsavedStreamingSpeakerId: string | undefined,
): string | undefined {
    if (matchedBubble) return matchedBubble.speakerId;
    return unsavedStreamingSpeakerId;
}

/** ルームビューで利用者が押して選んだペルソナと、選んだ時点の speakerId。 */
export interface RoomViewPick {
    personaId: string;
    speakerAtPick: string | undefined;
}

/** 押して選んだペルソナを、いまの speakerId のもとで保つか。speakerId が選んだ時点から
 * 変わったら null を返して選択を捨てる。捨てた選択は、speakerId が元の人に戻っても
 * 復活しない (呼び出し側が、返り値で state を置き換える)。 */
export function keepRoomViewPick(pick: RoomViewPick | null, speakerId: string | undefined): RoomViewPick | null {
    if (pick === null) return null;
    return pick.speakerAtPick === speakerId ? pick : null;
}

/** 「{名前} が喋っています」の印を出すか (決まり 2)。設定 (autoSwitch) が OFF の間は出さない。 */
export function showSpeakingIndicator(state: RoomViewAutoSwitchState): boolean {
    return state.enabled && state.autoSwitch && state.speaking && state.view === 'log';
}

/** 発話が続いているか (speaking が true か、終わった後の待ちの間)。 */
export function isUtteranceActive(state: RoomViewAutoSwitchState): boolean {
    return state.speaking || state.pendingReturnMs !== null;
}

// ---------- 過去のログを遡って読んでいる最中か (userBusy の一部) --------------------
// ログ (.chatArea) のスクロールには、利用者の操作によるものと、プログラムが起こすもの
// (メッセージが届いたときの自動スクロール、吹き出しが増えて高さが伸びた直後) がある。
// 「遡って読んでいる」を立てるのは、利用者の操作によるスクロールのときだけ。

/** 一番下からこの距離より上にあれば、遡って読んでいる位置とみなす。 */
export const SCROLLED_BACK_THRESHOLD_PX = 200;
/** 利用者のスクロールの操作 (wheel・touchmove・スクロールのキー) から、この時間の間に
 * 起きたスクロールを、利用者の操作によるものとみなす。 */
export const USER_SCROLL_INPUT_WINDOW_MS = 1000;

/** ログの上で、スクロールのキーとして数えるキー (KeyboardEvent.key)。 */
export const USER_SCROLL_KEYS: ReadonlySet<string> = new Set([
    'PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'Home', 'End', ' ',
]);

/** いまのスクロールが、利用者の操作によるものか。ポインタが押されている間
 * (スクロールバーのドラッグ) は続いているとみなす。lastInputAt は最後の操作の時刻
 * (操作が一度も無ければ null)。now と lastInputAt は同じ時計 (performance.now) の値。 */
export function isUserScrollInputActive(input: {
    pointerDown: boolean;
    lastInputAt: number | null;
    now: number;
}): boolean {
    if (input.pointerDown) return true;
    if (input.lastInputAt === null) return false;
    const elapsed = input.now - input.lastInputAt;
    return elapsed >= 0 && elapsed <= USER_SCROLL_INPUT_WINDOW_MS;
}

/** スクロールが起きたときの、次の「遡って読んでいる最中か」の値。
 * - 一番下から SCROLLED_BACK_THRESHOLD_PX 以内なら、誰が起こしたスクロールでも false。
 * - それより上で、利用者の操作によるスクロールなら true。
 * - それ以外 (プログラムが起こしたスクロール) では値を変えない。 */
export function nextScrolledBack(
    current: boolean,
    distanceFromBottom: number,
    userScrollInput: boolean,
): boolean {
    if (distanceFromBottom <= SCROLLED_BACK_THRESHOLD_PX) return false;
    if (userScrollInput) return true;
    return current;
}
