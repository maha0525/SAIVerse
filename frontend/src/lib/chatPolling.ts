export interface ActiveGameLike {
    region_id: string;
    inside?: boolean;
}

export type ChatViewTarget =
    | { kind: 'game'; regionId: string; viewKey: string }
    | { kind: 'building'; buildingId: string; viewKey: string };

export interface PollingCursor {
    viewKey: string;
    messageId: string;
}

/**
 * Determine which history source (building chat or game session log) is currently
 * displayed, together with a canonical viewKey for cursor binding.
 */
export function resolveChatViewTarget(
    buildingId: string | null | undefined,
    activeGame: ActiveGameLike | null | undefined,
    serverCurrentBuildingId: string | null | undefined,
    sessionLogPeek: boolean,
): ChatViewTarget | null {
    if (!buildingId) return null;
    const gameView = !!activeGame && (
        activeGame.inside
            ? buildingId === serverCurrentBuildingId
            : sessionLogPeek
    );
    if (gameView) {
        return {
            kind: 'game',
            regionId: activeGame.region_id,
            viewKey: `game:${activeGame.region_id}`,
        };
    }
    return {
        kind: 'building',
        buildingId,
        viewKey: `building:${buildingId}`,
    };
}

/**
 * Return the newest persisted message ID (ignoring optimistic temp-* IDs).
 */
export function pickNewestRealMessageId(
    messages: ReadonlyArray<{ id?: string }>,
): string | undefined {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const id = messages[i]?.id;
        if (id && !id.startsWith('temp-')) {
            return id;
        }
    }
    return undefined;
}

/**
 * Build a cursor bound to the given view, or null when the view has no real messages yet.
 */
export function buildPollingCursor(
    target: ChatViewTarget | null,
    messages: ReadonlyArray<{ id?: string }>,
): PollingCursor | null {
    if (!target) return null;
    const newestId = pickNewestRealMessageId(messages);
    if (!newestId) return null;
    return { viewKey: target.viewKey, messageId: newestId };
}

/**
 * Build the polling URL for the active view.
 * Uses `after` only when the cursor belongs to the exact same view; otherwise
 * omits `after` to fetch the latest page for an empty/fresh view.
 */
export function buildPollUrl(
    target: ChatViewTarget,
    cursor: PollingCursor | null,
    limit = 50,
): string {
    const afterId = cursor && cursor.viewKey === target.viewKey ? cursor.messageId : undefined;
    const params = new URLSearchParams();
    if (afterId) {
        params.append('after', afterId);
    }
    params.append('limit', String(limit));

    if (target.kind === 'game') {
        return `/api/world/regions/${encodeURIComponent(target.regionId)}/game/log?${params.toString()}`;
    }
    params.append('building_id', target.buildingId);
    return `/api/chat/history?${params.toString()}`;
}
