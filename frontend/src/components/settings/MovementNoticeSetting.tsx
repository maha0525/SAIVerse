"use client";

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useMovementNoticeSettings } from '@/hooks/useMovementNoticeSettings';
import { isGlobalMovementNoticeSavePending, saveGlobalMovementNoticeSetting } from '@/lib/movementNotices';
import styles from './MovementNoticeSetting.module.css';

/** Global display-only preference. Each Building may explicitly override it. */
export default function MovementNoticeSetting() {
    useLocale();
    const { settings, loadError, refresh, saving, saveError } = useMovementNoticeSettings();

    const save = async (value: boolean) => {
        if (!settings || isGlobalMovementNoticeSavePending()) return;
        try {
            await saveGlobalMovementNoticeSetting(value);
        } catch {
            // Shared state keeps the failure visible even if the modal was reopened.
        }
    };

    return (
        <div className={styles.container}>
            <div className={styles.row}>
                <div>
                    <label htmlFor="global-movement-notices" data-i18n="movementNotices.label" className={styles.label}>
                        {uiText('movementNotices.label')}
                    </label>
                    <p id="global-movement-notices-hint" className={styles.hint} data-i18n="movementNotices.globalHint">
                        {uiText('movementNotices.globalHint')}
                    </p>
                </div>
                <select
                    id="global-movement-notices"
                    aria-describedby="global-movement-notices-hint"
                    value={settings?.show_movement_notices === false ? 'hide' : 'show'}
                    disabled={!settings || saving}
                    onChange={event => void save(event.target.value === 'show')}
                >
                    <option value="show" data-i18n="movementNotices.show">{uiText('movementNotices.show')}</option>
                    <option value="hide" data-i18n="movementNotices.hide">{uiText('movementNotices.hide')}</option>
                </select>
            </div>
            {saving && <p role="status" data-i18n="movementNotices.saving">{uiText('movementNotices.saving')}</p>}
            {saveError && <p role="alert" className={styles.error} data-i18n="movementNotices.saveError">{uiText('movementNotices.saveError')}</p>}
            {loadError && (
                <div role="alert" className={styles.error}>
                    <span data-i18n="movementNotices.loadError">{uiText('movementNotices.loadError')}</span>{' '}
                    <button type="button" onClick={() => void refresh()} data-i18n="movementNotices.retry">{uiText('movementNotices.retry')}</button>
                </div>
            )}
        </div>
    );
}
