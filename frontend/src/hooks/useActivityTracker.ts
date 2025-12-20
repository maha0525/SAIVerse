"use client";

import { useEffect, useRef, useCallback } from 'react';

const HEARTBEAT_INTERVAL = 30000; // 30 seconds
const ACTIVITY_DEBOUNCE = 1000; // 1 second

/**
 * Custom hook to track user activity and send heartbeats to the backend.
 * - Tracks mouse, keyboard, scroll, and touch events
 * - Sends heartbeat every 30 seconds
 * - Notifies backend when page becomes hidden/visible
 */
export function useActivityTracker() {
    const lastActivityRef = useRef<number>(Date.now());
    const lastHeartbeatRef = useRef<number>(0);
    const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);

    // Debounced activity update
    const updateActivity = useCallback(() => {
        lastActivityRef.current = Date.now();
    }, []);

    // Send heartbeat to backend
    const sendHeartbeat = useCallback(async () => {
        const now = Date.now();
        // Debounce: don't send more often than every second
        if (now - lastHeartbeatRef.current < ACTIVITY_DEBOUNCE) {
            return;
        }
        lastHeartbeatRef.current = now;

        try {
            await fetch('/api/user/heartbeat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    last_interaction: new Date(lastActivityRef.current).toISOString()
                })
            });
        } catch (error) {
            console.error('[ActivityTracker] Heartbeat failed:', error);
        }
    }, []);

    // Handle page visibility change
    const handleVisibilityChange = useCallback(() => {
        if (document.hidden) {
            // Page is hidden - notify backend (use sendBeacon for reliability)
            try {
                navigator.sendBeacon(
                    '/api/user/visibility',
                    JSON.stringify({ visible: false })
                );
            } catch {
                // Fallback to fetch if sendBeacon fails
                fetch('/api/user/visibility', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ visible: false }),
                    keepalive: true
                }).catch(() => { });
            }
        } else {
            // Page is visible again - send heartbeat to restore online status
            sendHeartbeat();
        }
    }, [sendHeartbeat]);

    useEffect(() => {
        // Activity events to track
        const events = ['mousemove', 'keydown', 'click', 'scroll', 'touchstart', 'focus'];

        // Activity listener with debouncing
        let activityTimeout: NodeJS.Timeout | null = null;
        const debouncedActivity = () => {
            if (activityTimeout) {
                clearTimeout(activityTimeout);
            }
            activityTimeout = setTimeout(() => {
                updateActivity();
            }, 100);
        };

        // Attach event listeners
        events.forEach(event => {
            window.addEventListener(event, debouncedActivity, { passive: true });
        });

        // Set up heartbeat interval
        heartbeatIntervalRef.current = setInterval(sendHeartbeat, HEARTBEAT_INTERVAL);

        // Set up visibility change listener
        document.addEventListener('visibilitychange', handleVisibilityChange);

        // Initial heartbeat
        sendHeartbeat();

        // Handle page unload - try to notify backend
        const handleBeforeUnload = () => {
            try {
                navigator.sendBeacon(
                    '/api/user/visibility',
                    JSON.stringify({ visible: false })
                );
            } catch {
                // Best effort
            }
        };
        window.addEventListener('beforeunload', handleBeforeUnload);

        // Cleanup
        return () => {
            events.forEach(event => {
                window.removeEventListener(event, debouncedActivity);
            });

            if (heartbeatIntervalRef.current) {
                clearInterval(heartbeatIntervalRef.current);
            }

            if (activityTimeout) {
                clearTimeout(activityTimeout);
            }

            document.removeEventListener('visibilitychange', handleVisibilityChange);
            window.removeEventListener('beforeunload', handleBeforeUnload);
        };
    }, [updateActivity, sendHeartbeat, handleVisibilityChange]);
}
