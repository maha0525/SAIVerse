"use client";

import { RefObject, useEffect, useRef } from 'react';
import { MovementNoticeMessage, MovementNoticeSettings, shouldShowMovementMessage } from '@/lib/movementNotices';

interface ScrollMessage extends MovementNoticeMessage { id?: string }
interface Options {
    messages: readonly ScrollMessage[];
    settings: MovementNoticeSettings | null;
    currentBuildingId: string | null;
    ready: boolean;
    loadingOlder: boolean;
    endRef: RefObject<HTMLDivElement | null>;
}

const sameMessage = (a?: ScrollMessage, b?: ScrollMessage) => a === b || (!!a?.id && a.id === b?.id);

/** Hidden-only arrivals and preference changes must not move the reader's viewport. */
export function useMovementNoticeAutoScroll({ messages, settings, currentBuildingId, ready, loadingOlder, endRef }: Options): void {
    const previous = useRef(messages);
    useEffect(() => {
        const old = previous.current;
        previous.current = messages;
        // Policy/ready changes, edits to a streaming message, or prepended history are not new arrivals.
        if (!ready || loadingOlder || old === messages || messages.length <= old.length
            || sameMessage(old[old.length - 1], messages[messages.length - 1])) return;
        const newestVisible = messages.findLast(message => shouldShowMovementMessage(message, settings, currentBuildingId));
        if (!newestVisible || old.some(message => sameMessage(message, newestVisible))) return;
        endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }, [messages, settings, currentBuildingId, ready, loadingOlder, endRef]);
}
