import { apiFetch } from '@/i18n/api';
import { useEffect, useState } from 'react';

/** グローバル設定 (GlobalSettingsModal) が、喋っている間に自動でルームビューへ切り替えるかを
 * 保存したときに、window へ投げるイベント。detail は { enabled: boolean }。 */
export const ROOM_VIEW_AUTO_SWITCH_CHANGED = 'saiverse:room-view-auto-switch-changed';

export interface RoomViewAutoSwitchChange {
    enabled: boolean;
}

/** 保存が成功した後にだけ呼ぶ。開いているチャット画面が、再読み込み無しで新しい値を使う。 */
export function announceRoomViewAutoSwitchChange(enabled: boolean): void {
    window.dispatchEvent(new CustomEvent<RoomViewAutoSwitchChange>(ROOM_VIEW_AUTO_SWITCH_CHANGED, { detail: { enabled } }));
}

/** 喋っている間に自動でルームビューへ切り替えるか (GET /api/config/room-view-auto-switch、
 * docs/intent/screen_avatar.md 設計 1)。マウント時に一度だけ読み、読めるまで・読めなかったときは
 * false (OFF)。設定画面が値を変えたら ROOM_VIEW_AUTO_SWITCH_CHANGED で差し替える。 */
export function useRoomViewAutoSwitchSetting(): boolean {
    const [enabled, setEnabled] = useState(false);
    useEffect(() => {
        let cancelled = false;
        // 設定画面からの知らせを先に受けたら、後から届いた GET の値で上書きしない。
        let changedByEvent = false;
        apiFetch('/api/config/room-view-auto-switch')
            .then(res => (res.ok ? res.json() : null))
            .then(data => {
                if (!cancelled && !changedByEvent && data) setEnabled(data.enabled === true);
            })
            .catch(() => { /* 読めないときは OFF のまま */ });
        const onChange = (event: Event) => {
            const detail = (event as CustomEvent<RoomViewAutoSwitchChange>).detail;
            if (!detail || typeof detail.enabled !== 'boolean') return;
            changedByEvent = true;
            setEnabled(detail.enabled);
        };
        window.addEventListener(ROOM_VIEW_AUTO_SWITCH_CHANGED, onChange);
        return () => {
            cancelled = true;
            window.removeEventListener(ROOM_VIEW_AUTO_SWITCH_CHANGED, onChange);
        };
    }, []);
    return enabled;
}
