'use client';

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
const ITEM_TYPES: Array<{ value: string; label: string; hint: string }> = [
    { value: 'object', label: 'オブジェクト', hint: 'ふつうのもの。名前と説明だけを持ちます。' },
    { value: 'bag', label: 'バッグ', hint: '他のアイテムを入れられます。閉じている間、中のものは部屋の様子に出ません。' },
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
                `安全のため作成を中止しました。開いたときの部屋 (${openedBuildingIdRef.current ?? '不明'}) と` +
                `現在の部屋 (${buildingId}) が違います。いったん閉じてから開き直してください。`
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
                let detail = '作成に失敗しました';
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
            setError(err instanceof Error ? err.message : '作成に失敗しました');
        } finally {
            setSaving(false);
        }
    };

    const selectedHint = ITEM_TYPES.find(t => t.value === itemType)?.hint;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2>アイテムを作る</h2>
                    <button className={styles.closeBtn} onClick={onClose} title="閉じる">
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.content}>
                    <div className={styles.placeLine}>
                        置き場所: {buildingName || buildingId}
                    </div>

                    {error && <div className={styles.error}>{error}</div>}

                    <div className={styles.field}>
                        <label htmlFor="newItemName">名前</label>
                        <input
                            id="newItemName"
                            type="text"
                            value={name}
                            onChange={e => setName(e.target.value)}
                            onKeyDown={e => {
                                if (e.key === 'Enter') handleCreate();
                            }}
                            placeholder="例: 道具箱"
                            disabled={saving}
                            autoFocus
                        />
                    </div>

                    <div className={styles.field}>
                        <label htmlFor="newItemType">種類</label>
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
                        <label htmlFor="newItemDescription">説明</label>
                        <textarea
                            id="newItemDescription"
                            value={description}
                            onChange={e => setDescription(e.target.value)}
                            rows={3}
                            placeholder="どんなものか（空でも作れます）"
                            disabled={saving}
                        />
                        <small className={styles.hint}>
                            ここに書いた説明は、部屋の様子としてペルソナにも見えます。
                        </small>
                    </div>

                    <div className={styles.note}>
                        画像やドキュメントは、チャットにファイルを添付するとこの部屋のアイテムになります。
                    </div>

                    <div className={styles.actions}>
                        <button className={styles.cancelBtn} onClick={onClose} disabled={saving}>
                            キャンセル
                        </button>
                        <button
                            className={styles.createBtn}
                            onClick={handleCreate}
                            disabled={!canSubmit}
                            title={!trimmedName ? '名前を入れてください' : undefined}
                        >
                            {saving ? (
                                <>
                                    <Loader2 size={16} className={styles.spinner} />
                                    作成中...
                                </>
                            ) : (
                                <>
                                    <Plus size={16} />
                                    作成
                                </>
                            )}
                        </button>
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
