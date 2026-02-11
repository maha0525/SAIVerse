'use client';

import React, { useRef } from 'react';
import { createPortal } from 'react-dom';
import styles from './ModalOverlay.module.css';

interface ModalOverlayProps {
    /** Callback when the overlay is clicked properly (not from drag) */
    onClose: () => void;
    /** Modal content to render */
    children: React.ReactNode;
    /** Optional additional className for the overlay */
    className?: string;
}

/**
 * Shared modal overlay component that properly handles click-to-close behavior.
 * 
 * This component prevents accidental modal closure when users drag from input fields
 * and release outside the modal. It only closes when both mousedown AND mouseup
 * occur on the overlay itself.
 * 
 * Usage:
 * ```tsx
 * <ModalOverlay onClose={handleClose}>
 *     <div className={styles.modal}>
 *         // modal content
 *     </div>
 * </ModalOverlay>
 * ```
 */
export default function ModalOverlay({ onClose, children, className }: ModalOverlayProps) {
    // Track if mousedown started on the overlay (not dragged from inside modal)
    const overlayMouseDownRef = useRef(false);

    const handleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
        // Only mark as overlay click if the target is the overlay itself
        if (e.target === e.currentTarget) {
            overlayMouseDownRef.current = true;
        }
    };

    const handleMouseUp = (e: React.MouseEvent<HTMLDivElement>) => {
        // Only close if mousedown was on overlay AND mouseup is also on overlay
        if (overlayMouseDownRef.current && e.target === e.currentTarget) {
            onClose();
        }
        overlayMouseDownRef.current = false;
    };

    // Prevent touch events from bubbling (for mobile)
    const handleTouchStart = (e: React.TouchEvent) => e.stopPropagation();
    const handleTouchMove = (e: React.TouchEvent) => e.stopPropagation();

    const overlay = (
        <div
            className={`${styles.overlay} ${className || ''}`}
            onMouseDown={handleMouseDown}
            onMouseUp={handleMouseUp}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
        >
            {children}
        </div>
    );

    // Portal to document.body to escape any ancestor transforms
    // (which break position:fixed containment)
    if (typeof document !== 'undefined') {
        return createPortal(overlay, document.body);
    }
    return overlay;
}
