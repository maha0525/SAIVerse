'use client';

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
        case 'install': return '導入';
        case 'update': return '更新';
        case 'uninstall': return '削除';
    }
}

export default function AddonActionConfirmDialog({
    entry,
    operation,
    state,
    onCancel,
    onProceed,
}: Props) {
    const [deleteData, setDeleteData] = useState(false);
    const latestVer = entry.versions.find((v) => v.version === entry.latest);
    const opLabel = getOperationLabel(operation);

    return (
        <ModalOverlay onClose={onCancel}>
            <div className={styles.dialog} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <h3>{entry.display_name} を{opLabel}</h3>
                </div>

                <div className={styles.body}>
                    <p className={styles.description}>{entry.description}</p>

                    {operation === 'install' && latestVer && (
                        <>
                            <div className={styles.section}>
                                <div className={styles.label}>導入バージョン</div>
                                <div className={styles.value}>
                                    v{latestVer.version}
                                    <span className={styles.commit}>commit {latestVer.commit.slice(0, 7)}</span>
                                </div>
                            </div>
                            {(entry.requires.gpu || entry.requires.disk_gb) && (
                                <div className={styles.section}>
                                    <div className={styles.label}>システム要件</div>
                                    <ul className={styles.list}>
                                        {entry.requires.gpu && entry.requires.gpu !== 'none' && (
                                            <li>GPU: {entry.requires.gpu === 'required' ? '必須' : 'あれば加速'}</li>
                                        )}
                                        {entry.requires.disk_gb != null && (
                                            <li>ディスク: 約 {entry.requires.disk_gb} GB</li>
                                        )}
                                        {entry.requires.os && entry.requires.os.length > 0 && (
                                            <li>対応 OS: {entry.requires.os.join(', ')}</li>
                                        )}
                                    </ul>
                                </div>
                            )}
                            <div className={styles.note}>
                                <AlertTriangle size={14} />
                                <span>
                                    アドオンの setup スクリプト (依存パッケージのインストール / 外部リソースの取得等) が
                                    自動実行されます。完了まで数分かかる場合があります。
                                </span>
                            </div>
                        </>
                    )}

                    {operation === 'update' && state.kind === 'update_available' && latestVer && (
                        <>
                            <div className={styles.section}>
                                <div className={styles.label}>バージョン変更</div>
                                <div className={styles.value}>
                                    v{state.current_version} → v{state.new_version}
                                    <span className={styles.commit}>commit {latestVer.commit.slice(0, 7)}</span>
                                </div>
                            </div>
                            {latestVer.changelog_url && (
                                <div className={styles.section}>
                                    <div className={styles.label}>変更内容</div>
                                    <a
                                        className={styles.link}
                                        href={latestVer.changelog_url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                    >
                                        changelog を見る <ExternalLink size={11} />
                                    </a>
                                </div>
                            )}
                            <div className={styles.note}>
                                <AlertTriangle size={14} />
                                <span>
                                    setup_version の差分次第では setup スクリプトが再実行されます。
                                </span>
                            </div>
                        </>
                    )}

                    {operation === 'uninstall' && (
                        <>
                            <div className={styles.section}>
                                <div className={styles.label}>削除対象</div>
                                <ul className={styles.list}>
                                    <li>
                                        アドオン本体ディレクトリ (<code>expansion_data/{entry.id}/</code>)
                                    </li>
                                    <li>
                                        永続データディレクトリ
                                        (<code>~/.saiverse/user_data/addon_data/{entry.id}/</code>)
                                        — 下のチェックで指定
                                    </li>
                                </ul>
                            </div>
                            <label className={styles.checkboxRow}>
                                <input
                                    type="checkbox"
                                    checked={deleteData}
                                    onChange={(e) => setDeleteData(e.target.checked)}
                                />
                                <span>永続データも削除する (保存された参照音声・OAuth トークン等が消えます)</span>
                            </label>
                            <div className={styles.note}>
                                <AlertTriangle size={14} />
                                <span>削除後はカタログから再導入できます。</span>
                            </div>
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.btnSecondary} onClick={onCancel}>
                        キャンセル
                    </button>
                    <button
                        className={operation === 'uninstall' ? styles.btnDanger : styles.btnPrimary}
                        onClick={() => onProceed(operation === 'uninstall' ? deleteData : undefined)}
                    >
                        {opLabel}する
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
