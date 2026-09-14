'use client';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useEffect, useRef, useState } from 'react';
import { X, Plus, Loader2 } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './ItemCreateModal.module.css';

/** この画面から作れる種類。
 *
 * 画像 (picture) とドキュメント (document) をここに出さないのは、
 * その二つは中身のファイルが無いと開けないアイテムになるから
 * (ファイル無しで作ると ItemModal を開いたときに読み込みエラーになる)。
 * ユーザーがその二つを作る道は既にあり、チャットにファイルを添付すると
 * 今いる Building のアイテムとして置かれる (api/routes/chat.py の
 * _store_image_attachment / _store_document_attachment)。
 */
const ITEM_TYPES: Array<{ value: string; readonly label: string; readonly hint: string }> = [
    { value: 'object', get label() { return uiText("components.ItemCreateModal.text001"); }, get hint() { return uiText("components.ItemCreateModal.text002"); } },
    { value: 'bag', get label() { return uiText("components.ItemCreateModal.text003"); }, get hint() { return uiText("components.ItemCreateModal.text004"); } },
];

interface ItemCreateModalProps {
    isOpen: boolean;
    onClose: () => void;
    /** 作成先の Building ID。親 (RightSidebar) が把握しているものを必ず渡す。
     * server-global の user_current_building_id にフォールバックさせない
     * (2026-04-30 エリス上書き事故 / feedback_modal_id_integrity.md)。 */
    buildingId: string;
    /** ヘッダーに出す Building の表示名 (任意)。 */
    buildingName?: string;
    /** 作成に成功したとき。親はアイテム一覧を再読込する。 */
    onCreated?: () => void;
}

export default function ItemCreateModal({
    isOpen,
    onClose,
    buildingId,
    buildingName,
    onCreated,
}: ItemCreateModalProps) {
    useLocale();
    const [name, setName] = useState('');
    const [itemType, setItemType] = useState('object');
    const [description, setDescription] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // モーダルを開いた時点の Building を覚えておき、送信時に今の buildingId と
    // 食い違っていたら書き込みを拒否する (フォームの中身は古い部屋のもの、
    // 保存先だけ新しい部屋、という状態を作らないため)。
    const openedBuildingIdRef = useRef<string | null>(null);

    useEffect(() => {
        if (!isOpen) return;
        setName('');
        setItemType('object');
        setDescription('');
        setError(null);
        setSaving(false);
        openedBuildingIdRef.current = buildingId;
    }, [isOpen, buildingId]);

    if (!isOpen) return null;

    const trimmedName = name.trim();
    const canSubmit = !!trimmedName && !saving && !!buildingId;

    const handleCreate = async () => {
        if (!canSubmit) return;
        if (openedBuildingIdRef.current !== buildingId) {
            setError(
                uiText("components.ItemCreateModal.text005", {
                    p1: openedBuildingIdRef.current ?? uiText("components.ItemCreateModal.text006"),
                    p2: buildingId,
                })
            );
            return;
        }

        setSaving(true);
        setError(null);
        try {
            const res = await fetch('/api/world/items', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: trimmedName,
                    item_type: itemType,
                    description: description.trim(),
                    owner_kind: 'building',
                    owner_id: buildingId,
                    creator_id: 'user',
                    source_context: JSON.stringify({ source: 'room_panel' }),
                }),
            });
            if (!res.ok) {
                let detail = uiText("components.ItemCreateModal.text007");
                try {
                    const data = await res.json();
                    if (data?.detail) detail = data.detail;
                } catch {
                    /* レスポンスが JSON でないときは既定の文言のまま */
                }
                throw new Error(detail);
            }
            onCreated?.();
            onClose();
        } catch (err) {
            console.error('Failed to create item', err);
            setError(err instanceof Error ? err.message : uiText("components.ItemCreateModal.text007"));
        } finally {
            setSaving(false);
        }
    };

    const selectedHint = ITEM_TYPES.find(t => t.value === itemType)?.hint;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 data-i18n="components.ItemCreateModal.text008">{uiText("components.ItemCreateModal.text008")}</h2>
                    <button data-i18n="components.ItemCreateModal.text009" className={styles.closeBtn} onClick={onClose} title={uiText("components.ItemCreateModal.text009")}>
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.content}>
                    <div data-i18n="components.ItemCreateModal.text010" className={styles.placeLine}>
                        {uiText("components.ItemCreateModal.text010", { p1: buildingName || buildingId })}
                    </div>

                    {error && <div className={styles.error}>{error}</div>}

                    <div className={styles.field}>
                        <label data-i18n="components.ItemCreateModal.text011" htmlFor="newItemName">{uiText("components.ItemCreateModal.text011")}</label>
                        <input
                            id="newItemName"
                            type="text"
                            value={name}
                            onChange={e => setName(e.target.value)}
                            onKeyDown={e => {
                                if (e.key === 'Enter') handleCreate();
                            }}
                            placeholder={uiText("components.ItemCreateModal.text012")}
                            disabled={saving}
                            autoFocus
                        />
                    </div>

                    <div className={styles.field}>
                        <label data-i18n="components.ItemCreateModal.text013" htmlFor="newItemType">{uiText("components.ItemCreateModal.text013")}</label>
                        <select
                            id="newItemType"
                            value={itemType}
                            onChange={e => setItemType(e.target.value)}
                            disabled={saving}
                        >
                            {ITEM_TYPES.map(t => (
                                <option key={t.value} value={t.value}>{t.label}</option>
                            ))}
                        </select>
                        {selectedHint && <small className={styles.hint}>{selectedHint}</small>}
                    </div>

                    <div className={styles.field}>
                        <label data-i18n="components.ItemCreateModal.text014" htmlFor="newItemDescription">{uiText("components.ItemCreateModal.text014")}</label>
                        <textarea
                            id="newItemDescription"
                            value={description}
                            onChange={e => setDescription(e.target.value)}
                            rows={3}
                            placeholder={uiText("components.ItemCreateModal.text015")}
                            disabled={saving}
                        />
                        <small data-i18n="components.ItemCreateModal.text016" className={styles.hint}>
                            {uiText("components.ItemCreateModal.text016")}
                        </small>
                    </div>

                    <div data-i18n="components.ItemCreateModal.text017" className={styles.note}>
                        {uiText("components.ItemCreateModal.text017")}
                    </div>

                    <div className={styles.actions}>
                        <button data-i18n="components.ItemCreateModal.text018" className={styles.cancelBtn} onClick={onClose} disabled={saving}>
                            {uiText("components.ItemCreateModal.text018")}
                        </button>
                        <button
                            data-i18n="components.ItemCreateModal.text019 components.ItemCreateModal.text020 components.ItemCreateModal.text021"
                            className={styles.createBtn}
                            onClick={handleCreate}
                            disabled={!canSubmit}
                            title={!trimmedName ? uiText("components.ItemCreateModal.text019") : undefined}
                        >
                            {saving ? (
                                <>
                                    <Loader2 size={16} className={styles.spinner} />
                                    {uiText("components.ItemCreateModal.text020")}
                                </>
                            ) : (
                                <>
                                    <Plus size={16} />
                                    {uiText("components.ItemCreateModal.text021")}
                                </>
                            )}
                        </button>
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
