import { useCallback, useEffect, useReducer } from 'react';
import {
    initialRoomViewAutoSwitchState,
    isUtteranceActive,
    nextRoomViewAutoSwitchState,
    showSpeakingIndicator,
    type RoomViewDisplay,
} from '@/lib/roomViewAutoSwitch';

interface UseRoomViewAutoSwitchOptions {
    /** overlayMode か (docs/intent/screen_avatar.md 設計 1 の「右サイドバーを出せない幅」)。 */
    enabled: boolean;
    speaking: boolean;
    audioPlaying: boolean;
    /** 利用者が入力している最中か、過去のログを遡って読んでいる最中。 */
    userBusy: boolean;
    /** 見ている部屋 (currentBuildingId)。変わったら 'log' に戻す。 */
    resetKey: string | null;
}

/** ログとルームビューの切り替え。決まりは roomViewAutoSwitch.ts にあり、
 * ここは出来事を渡すことと、待ち時間のタイマーだけを受け持つ。 */
export function useRoomViewAutoSwitch({ enabled, speaking, audioPlaying, userBusy, resetKey }: UseRoomViewAutoSwitchOptions): {
    view: RoomViewDisplay;
    showIndicator: boolean;
    utteranceActive: boolean;
    toggle: () => void;
    pressIndicator: () => void;
} {
    const [state, dispatch] = useReducer(nextRoomViewAutoSwitchState, undefined, initialRoomViewAutoSwitchState);

    useEffect(() => {
        dispatch({ type: 'signal', enabled, speaking, audioPlaying, userBusy });
    }, [enabled, speaking, audioPlaying, userBusy]);

    useEffect(() => {
        dispatch({ type: 'reset' });
    }, [resetKey]);

    const { pendingReturnMs, pendingToken } = state;
    useEffect(() => {
        if (pendingReturnMs === null) return;
        const timer = setTimeout(() => {
            dispatch({ type: 'returnTimerFired', token: pendingToken });
        }, pendingReturnMs);
        return () => clearTimeout(timer);
    }, [pendingReturnMs, pendingToken]);

    const toggle = useCallback(() => dispatch({ type: 'toggle' }), []);
    const pressIndicator = useCallback(() => dispatch({ type: 'pressIndicator' }), []);

    return {
        view: state.enabled ? state.view : 'log',
        showIndicator: showSpeakingIndicator(state),
        utteranceActive: isUtteranceActive(state),
        toggle,
        pressIndicator,
    };
}
