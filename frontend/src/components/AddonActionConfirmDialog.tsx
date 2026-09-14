'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState } from 'react';
import { AlertTriangle, ExternalLink } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './AddonActionConfirmDialog.module.css';
import type { CatalogOperation } from './AddonInstallProgressDialog';

interface RegistryVersionEntry {
    version: string;
    commit: string;
    setup_version: number;
    changelog_url?: string | null;
}

interface RegistryAddonEntry {
    id: string;
    display_name: string;
    description: string;
    latest: string;
    versions: RegistryVersionEntry[];
    requires: {
        gpu?: string | null;
        disk_gb?: number | null;
        os?: string[];
    };
}

type RowState =
    | { kind: 'not_installed' }
    | { kind: 'installed_latest'; current_version: string }
    | { kind: 'update_available'; current_version: string; new_version: string };

interface Props {
    entry: RegistryAddonEntry;
    operation: CatalogOperation;
    state: RowState;
    onCancel: () => void;
    onProceed: (deleteData: boolean | undefined) => void;
}

function getOperationLabel(op: CatalogOperation): string {
    switch (op) {
        case 'install': return uiText("components.AddonActionConfirmDialog.text001");
        case 'update': return uiText("components.AddonActionConfirmDialog.text002");
        case 'uninstall': return uiText("components.AddonActionConfirmDialog.text003");
    }
}

export default function AddonActionConfirmDialog({
    entry,
    operation,
    state,
    onCancel,
    onProceed,
}: Props) {
    useLocale();
    const [deleteData, setDeleteData] = useState(false);
    const latestVer = entry.versions.find((v) => v.version === entry.latest);
    const opLabel = getOperationLabel(operation);

    return (
        <ModalOverlay onClose={onCancel}>
            <div className={styles.dialog} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <h3 data-i18n="components.AddonActionConfirmDialog.text004">{entry.display_name}{uiText("components.AddonActionConfirmDialog.text004")}{opLabel}</h3>
                </div>

                <div className={styles.body}>
                    <p className={styles.description}>{entry.description}</p>

                    {operation === 'install' && latestVer && (
                        <>
                            <div className={styles.section}>
                                <div data-i18n="components.AddonActionConfirmDialog.text005" className={styles.label}>{uiText("components.AddonActionConfirmDialog.text005")}</div>
                                <div className={styles.value}>
                                    v{latestVer.version}
                                    <span className={styles.commit}>{uiText("components.AddonActionConfirmDialog.label001")}{latestVer.commit.slice(0, 7)}</span>
                                </div>
                            </div>
                            {(entry.requires.gpu || entry.requires.disk_gb) && (
                                <div className={styles.section}>
                                    <div data-i18n="components.AddonActionConfirmDialog.text006" className={styles.label}>{uiText("components.AddonActionConfirmDialog.text006")}</div>
                                    <ul className={styles.list}>
                                        {entry.requires.gpu && entry.requires.gpu !== 'none' && (
                                            <li data-i18n="components.AddonActionConfirmDialog.text007 components.AddonActionConfirmDialog.text008">{uiText("components.AddonActionConfirmDialog.label002")}{entry.requires.gpu === 'required' ? uiText("components.AddonActionConfirmDialog.text007") : uiText("components.AddonActionConfirmDialog.text008")}</li>
                                        )}
                                        {entry.requires.disk_gb != null && (
                                            <li data-i18n="components.AddonActionConfirmDialog.text009">{uiText("components.AddonActionConfirmDialog.text009")}{entry.requires.disk_gb} {uiText("components.AddonActionConfirmDialog.label003")}</li>
                                        )}
                                        {entry.requires.os && entry.requires.os.length > 0 && (
                                            <li data-i18n="components.AddonActionConfirmDialog.text010">{uiText("components.AddonActionConfirmDialog.text010")}{entry.requires.os.join(', ')}</li>
                                        )}
                                    </ul>
                                </div>
                            )}
                            <div className={styles.note}>
                                <AlertTriangle size={14} />
                                <span data-i18n="components.AddonActionConfirmDialog.text011">{uiText("components.AddonActionConfirmDialog.text011")}</span>
                            </div>
                        </>
                    )}

                    {operation === 'update' && state.kind === 'update_available' && latestVer && (
                        <>
                            <div className={styles.section}>
                                <div data-i18n="components.AddonActionConfirmDialog.text012" className={styles.label}>{uiText("components.AddonActionConfirmDialog.text012")}</div>
                                <div className={styles.value}>
                                    v{state.current_version} → v{state.new_version}
                                    <span className={styles.commit}>{uiText("components.AddonActionConfirmDialog.label004")}{latestVer.commit.slice(0, 7)}</span>
                                </div>
                            </div>
                            {latestVer.changelog_url && (
                                <div className={styles.section}>
                                    <div data-i18n="components.AddonActionConfirmDialog.text013" className={styles.label}>{uiText("components.AddonActionConfirmDialog.text013")}</div>
                                    <a data-i18n="components.AddonActionConfirmDialog.text014"
                                        className={styles.link}
                                        href={latestVer.changelog_url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                    >{uiText("components.AddonActionConfirmDialog.text014")}<ExternalLink size={11} />
                                    </a>
                                </div>
                            )}
                            <div className={styles.note}>
                                <AlertTriangle size={14} />
                                <span data-i18n="components.AddonActionConfirmDialog.text015">{uiText("components.AddonActionConfirmDialog.text015")}</span>
                            </div>
                        </>
                    )}

                    {operation === 'uninstall' && (
                        <>
                            <div className={styles.section}>
                                <div data-i18n="components.AddonActionConfirmDialog.text016" className={styles.label}>{uiText("components.AddonActionConfirmDialog.text016")}</div>
                                <ul className={styles.list}>
                                    <li data-i18n="components.AddonActionConfirmDialog.text017">{uiText("components.AddonActionConfirmDialog.text017")}<code>{uiText("components.AddonActionConfirmDialog.label005")}{entry.id}/</code>)
                                    </li>
                                    <li data-i18n="components.AddonActionConfirmDialog.text018 components.AddonActionConfirmDialog.text019">{uiText("components.AddonActionConfirmDialog.text018")}<code>{uiText("components.AddonActionConfirmDialog.label006")}{entry.id}/</code>{uiText("components.AddonActionConfirmDialog.text019")}</li>
                                </ul>
                            </div>
                            <label className={styles.checkboxRow}>
                                <input
                                    type="checkbox"
                                    checked={deleteData}
                                    onChange={(e) => setDeleteData(e.target.checked)}
                                />
                                <span data-i18n="components.AddonActionConfirmDialog.text020">{uiText("components.AddonActionConfirmDialog.text020")}</span>
                            </label>
                            <div className={styles.note}>
                                <AlertTriangle size={14} />
                                <span data-i18n="components.AddonActionConfirmDialog.text021">{uiText("components.AddonActionConfirmDialog.text021")}</span>
                            </div>
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button data-i18n="components.AddonActionConfirmDialog.text022" className={styles.btnSecondary} onClick={onCancel}>{uiText("components.AddonActionConfirmDialog.text022")}</button>
                    <button data-i18n="components.AddonActionConfirmDialog.text023"
                        className={operation === 'uninstall' ? styles.btnDanger : styles.btnPrimary}
                        onClick={() => onProceed(operation === 'uninstall' ? deleteData : undefined)}
                    >
                        {opLabel}{uiText("components.AddonActionConfirmDialog.text023")}</button>
                </div>
            </div>
        </ModalOverlay>
    );
}
