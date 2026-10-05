import { apiFetch } from '@/i18n/api';

// Keep an in-flight form save alive across Close/reopen and Building navigation.
// Reopened forms wait before reading, so they cannot resubmit a pre-save snapshot.
const pending = new Map<string, Promise<Response>>();

export function pendingBuildingSettingsSave(buildingId: string): Promise<Response> | undefined {
    return pending.get(buildingId);
}

export function saveBuildingSettings(buildingId: string, body: Record<string, unknown>): Promise<Response> {
    if (pending.has(buildingId)) throw new Error('Building settings save already in progress');
    const request = apiFetch(`/api/world/buildings/${encodeURIComponent(buildingId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).finally(() => { pending.delete(buildingId); });
    pending.set(buildingId, request);
    return request;
}
