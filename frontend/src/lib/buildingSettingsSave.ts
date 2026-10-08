import { apiFetch } from '@/i18n/api';

export interface BuildingSettingsSaveResult {
    ok: boolean;
    error?: string;
}

// One save includes reading the error body, not just receiving HTTP headers.
// Every form instance waits on that same operation before reading or saving again.
const pending = new Map<string, Promise<BuildingSettingsSaveResult>>();

export function pendingBuildingSettingsSave(buildingId: string): Promise<BuildingSettingsSaveResult> | undefined {
    return pending.get(buildingId);
}

export function saveBuildingSettings(buildingId: string, body: Record<string, unknown>): Promise<BuildingSettingsSaveResult> {
    if (pending.has(buildingId)) throw new Error('Building settings save already in progress');
    const request = apiFetch(`/api/world/buildings/${encodeURIComponent(buildingId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(async response => {
        if (response.ok) return { ok: true };
        const data = await response.json().catch(() => null);
        return { ok: false, error: typeof data?.detail === 'string' ? data.detail : undefined };
    }).finally(() => { pending.delete(buildingId); });
    pending.set(buildingId, request);
    return request;
}
