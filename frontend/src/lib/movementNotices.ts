import { apiFetch } from '@/i18n/api';

/** Presentation settings only. Never remove messages from the history/cursor state. */
export interface MovementNoticeSettings {
    show_movement_notices: boolean;
    building_overrides: Record<string, boolean>;
}

export interface MovementNoticeMessage {
    role: string;
    is_movement_notice?: boolean;
    building_id?: string | null;
}

export function shouldShowMovementMessage(
    message: MovementNoticeMessage,
    settings: MovementNoticeSettings | null,
    currentBuildingId?: string | null,
): boolean {
    // Missing classification (old records/servers) is deliberately visible.
    if (message.is_movement_notice !== true || !['host', 'system'].includes(message.role)) return true;
    const buildingId = message.building_id ?? currentBuildingId;
    const override = buildingId ? settings?.building_overrides[buildingId] : undefined;
    return typeof override === 'boolean' ? override : settings?.show_movement_notices ?? true;
}

export const MOVEMENT_NOTICES_CHANGED = 'saiverse:movement-notices-changed';

export type MovementNoticeChange =
    | { settings: MovementNoticeSettings }
    | { globalValue: boolean }
    | { buildingId: string; value: boolean | null };

/** Called only after a successful save; other tabs/clients catch up via read-only refresh. */
export function announceMovementNoticeChange(change: MovementNoticeChange): void {
    window.dispatchEvent(new CustomEvent<MovementNoticeChange>(MOVEMENT_NOTICES_CHANGED, { detail: change }));
}

export function applyMovementNoticeChange(
    settings: MovementNoticeSettings | null,
    change: MovementNoticeChange,
): MovementNoticeSettings | null {
    if ('settings' in change) return change.settings;
    if (!settings) return null;
    if ('globalValue' in change) return { ...settings, show_movement_notices: change.globalValue };
    const overrides = { ...settings.building_overrides };
    if (change.value === null) delete overrides[change.buildingId];
    else overrides[change.buildingId] = change.value;
    return { ...settings, building_overrides: overrides };
}

// Module-scoped, rather than modal-scoped: closing/reopening must not start a
// second write while the previous choice is still committing or awaiting its response.
export const MOVEMENT_NOTICES_SAVE_STATE_CHANGED = 'saiverse:movement-notices-save-state-changed';
let globalSavePending = false;
let globalSaveFailed = false;

export function isGlobalMovementNoticeSavePending(): boolean {
    return globalSavePending;
}

export function didGlobalMovementNoticeSaveFail(): boolean {
    return globalSaveFailed;
}

export async function saveGlobalMovementNoticeSetting(value: boolean): Promise<void> {
    if (globalSavePending) return;
    globalSavePending = true;
    globalSaveFailed = false;
    window.dispatchEvent(new Event(MOVEMENT_NOTICES_SAVE_STATE_CHANGED));
    try {
        const response = await apiFetch('/api/config/movement-notices', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ show_movement_notices: value }),
        });
        if (!response.ok) throw new Error(`Movement notice save: ${response.status}`);
        const settings: MovementNoticeSettings = await response.json();
        // The response's override snapshot can predate a concurrent Building save.
        // Publish only the value this request owns; GET reconciles the full snapshot afterward.
        announceMovementNoticeChange({ globalValue: settings.show_movement_notices });
    } catch (error) {
        globalSaveFailed = true;
        throw error;
    } finally {
        globalSavePending = false;
        window.dispatchEvent(new Event(MOVEMENT_NOTICES_SAVE_STATE_CHANGED));
    }
}
