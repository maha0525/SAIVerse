'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState } from 'react';
import { AlertCircle, AlertTriangle, ExternalLink, Loader2 } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './AddonActionConfirmDialog.module.css';
import type { CatalogOperation } from './AddonInstallProgressDialog';
import AddonSetupQuestions, {
    SetupAnswers,
    SetupPlan,
    answersComplete,
    hasNewSelection,
    initialAnswers,
} from './AddonSetupQuestions';

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

/**
 * 導入時の質問 (prepare / options の API の答え) の読み込み状態。
 * - loading: API を待っている (承認ボタンは押せない)
 * - error:   読み込めなかった (承認ボタンは押せない)
 * - ready:   needsSetup が false なら setup は走らないので質問を出さない (更新)
 */
export type SetupLoadState =
    | { status: 'loading' }
    | { status: 'error'; message: string }
    | { status: 'ready'; plan: SetupPlan; needsSetup: boolean };

export interface ConfirmProceedResult {
    /** uninstall のときだけ */
    deleteData?: boolean;
    /** install / update / options のときだけ */
    answers?: SetupAnswers;
}

interface Props {
    /** カタログの行 (install / update / uninstall)。options では無い */
    entry?: RegistryAddonEntry;
    /** 見出しに出す名前。省略時は entry.display_name */
    displayName?: string;
    /** 省略時は entry.description */
    description?: string;
    operation: CatalogOperation;
    state?: RowState;
    /** install / update / options で必須。uninstall では渡さない */
    setup?: SetupLoadState;
    onCancel: () => void;
    onProceed: (result: ConfirmProceedResult) => void;
}

function getOperationLabel(op: CatalogOperation): string {
    switch (op) {
        case 'install': return uiText("components.AddonActionConfirmDialog.text001");
        case 'update': return uiText("components.AddonActionConfirmDialog.text002");
        case 'uninstall': return uiText("components.AddonActionConfirmDialog.text003");
        case 'options': return uiText("components.AddonActionConfirmDialog.text024");
    }
}

export default function AddonActionConfirmDialog({
    entry,
    displayName,
    description,
    operation,
    state,
    setup,
    onCancel,
    onProceed,
}: Props) {
    useLocale();
    const [deleteData, setDeleteData] = useState(false);
    // 利用者が触るまでは null (= 質問の初期値をそのまま使う)。setup が
    // loading → ready に変わったときに初期値を取り直す必要がないように、
    // 触った時点で初めて state に持つ。
    const [editedAnswers, setEditedAnswers] = useState<SetupAnswers | null>(null);
    const latestVer = entry?.versions.find((v) => v.version === entry.latest);
    const opLabel = getOperationLabel(operation);
    const title = displayName ?? entry?.display_name ?? '';
    const descText = description ?? entry?.description ?? '';

    const needsSetupInfo = operation !== 'uninstall';
    const readyPlan = setup?.status === 'ready' ? setup.plan : null;
    const answers: SetupAnswers = editedAnswers ?? (readyPlan ? initialAnswers(readyPlan) : {});
    // 質問を出すのは、setup が走り、かつ質問があるときだけ。質問の無い
    // アドオン (Elyth・X・stackchan) は従来どおりの見た目のまま。
    const showQuestions = !!readyPlan
        && setup?.status === 'ready'
        && (operation !== 'update' || setup.needsSetup)
        && readyPlan.questions.length > 0;

    let canProceed = true;
    if (needsSetupInfo) {
        if (setup?.status !== 'ready') {
            canProceed = false;
        } else if (showQuestions && readyPlan) {
            canProceed = answersComplete(readyPlan, answers)
                && (operation !== 'options' || hasNewSelection(readyPlan, answers));
        } else if (operation === 'options') {
            // 質問が無ければ、足せる選択肢も無い
            canProceed = false;
        }
    }

    const handleProceed = () => {
        if (!canProceed) return;
        if (operation === 'uninstall') {
            onProceed({ deleteData });
            return;
        }
        // 質問を出さなかったとき (質問が無い / 更新で setup が走らない) は空の答え
        onProceed({ answers: showQuestions ? answers : {} });
    };

    return (
        <ModalOverlay onClose={onCancel}>
            <div className={styles.dialog} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    {operation === 'options' ? (
                        <h3 data-i18n="components.AddonActionConfirmDialog.text025">{uiText("components.AddonActionConfirmDialog.text025", { p1: title })}</h3>
                    ) : (
                        <h3 data-i18n="components.AddonActionConfirmDialog.text004">{title}{uiText("components.AddonActionConfirmDialog.text004")}{opLabel}</h3>
                    )}
                </div>

                <div className={styles.body}>
                    {descText && <p className={styles.description}>{descText}</p>}

                    {operation === 'install' && entry && latestVer && (
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

                    {operation === 'update' && state?.kind === 'update_available' && latestVer && (
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

                    {operation === 'uninstall' && entry && (
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

                    {operation === 'options' && (
                        <div className={styles.note}>
                            <AlertTriangle size={14} />
                            <span data-i18n="components.AddonActionConfirmDialog.text028">{uiText("components.AddonActionConfirmDialog.text028")}</span>
                        </div>
                    )}

                    {needsSetupInfo && setup?.status === 'loading' && (
                        <div data-i18n="components.AddonActionConfirmDialog.text026" className={styles.loadingRow}>
                            <Loader2 size={14} className={styles.spin} />{uiText("components.AddonActionConfirmDialog.text026")}</div>
                    )}

                    {needsSetupInfo && setup?.status === 'error' && (
                        <div className={styles.errorBox}>
                            <AlertCircle size={14} />
                            <div>
                                <div data-i18n="components.AddonActionConfirmDialog.text027">{uiText("components.AddonActionConfirmDialog.text027")}</div>
                                <div className={styles.errorDetail}>{setup.message}</div>
                            </div>
                        </div>
                    )}

                    {showQuestions && readyPlan && (
                        <AddonSetupQuestions
                            plan={readyPlan}
                            answers={answers}
                            onChange={setEditedAnswers}
                            stepListMode={operation === 'options' ? 'added' : 'all'}
                        />
                    )}
                </div>

                <div className={styles.footer}>
                    <button data-i18n="components.AddonActionConfirmDialog.text022" className={styles.btnSecondary} onClick={onCancel}>{uiText("components.AddonActionConfirmDialog.text022")}</button>
                    <button data-i18n="components.AddonActionConfirmDialog.text023"
                        className={operation === 'uninstall' ? styles.btnDanger : styles.btnPrimary}
                        onClick={handleProceed}
                        disabled={!canProceed}
                    >
                        {opLabel}{uiText("components.AddonActionConfirmDialog.text023")}</button>
                </div>
            </div>
        </ModalOverlay>
    );
}
