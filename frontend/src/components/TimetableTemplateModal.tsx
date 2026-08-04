import React, { useEffect, useRef, useState } from 'react';
import { X, Save, Loader2, CalendarClock, Plus, Trash2, ArrowUp, ArrowDown } from 'lucide-react';
import styles from './TimetableTemplateModal.module.css';
import ModalOverlay from './common/ModalOverlay';

/**
 * 習慣テンプレート編集モーダル (時間割改修 T2b、timetable_redesign.md §5.1/§5.3)。
 *
 * ペルソナの一日のコマ並びの「枠」をユーザーが決める画面。ここで保存した
 * テンプレートがある朝は、ペルソナは時間割をゼロから組まず、この枠の
 * 「朝に決める」(= 穴) の部分だけを自分で埋めて一日を始める。
 *
 * テンプレートを書き換える経路はこの画面 (PUT /timetable-template) だけで、
 * ペルソナの LLM 出力から直接変わることはない (intent §9-1)。
 *
 * 置き場所は LifeSettingsModal と同じ「PersonaMenu 起点の兄弟モーダル」:
 * ライフ設定 (起床・就寝・予算 = 一日の外枠) の隣に、一日の中身の枠として並ぶ。
 */

/** 保存済みテンプレートのコマ 1 件 (API の TemplateSlotModel と同形)。 */
interface TemplateSlot {
    start: string;
    kind?: string | null;
    title?: string | null;
    facility?: string | null;
    note?: string | null;
    budget_rounds?: number | null;
    ref?: string | null;
}

/** 編集フォーム上のコマ 1 行。空文字 = 「朝に決める」(保存時に null で送る)。 */
interface SlotRow {
    start: string;
    kind: string;
    title: string;
    facility: string;
    note: string;
    budget: string;
    /** UI では編集しないが、保存済みの値を落とさないために持ち回る。 */
    ref: string | null;
}

interface SlotKindInfo {
    id: string;
    name: string;
    execution_type: string;
    description: string;
    builtin: boolean;
}

interface FacilityOption {
    id: string;
    name: string;
}

interface TimetableTemplateModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName?: string;
}

/** 「朝に決める」を表すセレクト・入力の空値。 */
const MORNING_CHOICE = '';
const MORNING_LABEL = '朝に決める';

function toRow(slot: TemplateSlot): SlotRow {
    return {
        start: slot.start || '',
        kind: slot.kind || MORNING_CHOICE,
        title: slot.title || '',
        facility: slot.facility || MORNING_CHOICE,
        note: slot.note || '',
        budget: slot.budget_rounds != null ? String(slot.budget_rounds) : '',
        ref: slot.ref || null,
    };
}

function toPayloadSlot(row: SlotRow): TemplateSlot {
    const slot: TemplateSlot = { start: row.start };
    if (row.kind !== MORNING_CHOICE) slot.kind = row.kind;
    if (row.title.trim() !== '') slot.title = row.title;
    if (row.facility !== MORNING_CHOICE) slot.facility = row.facility;
    if (row.note.trim() !== '') slot.note = row.note;
    if (row.budget.trim() !== '') slot.budget_rounds = parseInt(row.budget, 10);
    if (row.ref) slot.ref = row.ref;
    return slot;
}

/** 追加行の初期時刻: 最後の行の 1 時間後 (なければ 08:00)。 */
function nextStart(rows: SlotRow[]): string {
    const last = rows[rows.length - 1];
    if (!last || !/^([01]\d|2[0-3]):([0-5]\d)$/.test(last.start)) return '08:00';
    const minutes = parseInt(last.start.slice(0, 2), 10) * 60 + parseInt(last.start.slice(3, 5), 10);
    const next = (minutes + 60) % (24 * 60);
    const hh = String(Math.floor(next / 60)).padStart(2, '0');
    const mm = String(next % 60).padStart(2, '0');
    return `${hh}:${mm}`;
}

export default function TimetableTemplateModal({ isOpen, onClose, personaId, personaName }: TimetableTemplateModalProps) {
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);

    // サーバーにテンプレートが保存されているか (未設定なら説明 + 作成ボタン)
    const [hasTemplate, setHasTemplate] = useState(false);
    // 「作成」を押して編集を始めたか (未設定 → 編集開始の遷移用)
    const [isEditing, setIsEditing] = useState(false);
    const [rows, setRows] = useState<SlotRow[]>([]);
    const [enabled, setEnabled] = useState(true);
    const [kinds, setKinds] = useState<SlotKindInfo[]>([]);
    const [facilities, setFacilities] = useState<FacilityOption[]>([]);
    // 保存失敗 (422 等) の理由。保存し直すまで画面に出したままにする
    const [errorMessage, setErrorMessage] = useState<string | null>(null);
    const [savedMessage, setSavedMessage] = useState<string | null>(null);

    // LifeSettingsModal と同じ整合性ガード (feedback_modal_id_integrity.md):
    // ロード元 personaId と保存先 personaId が一致するときだけ保存を許可する。
    const [loadedPersonaId, setLoadedPersonaId] = useState<string | null>(null);
    const personaIdRef = useRef<string>(personaId);
    personaIdRef.current = personaId;
    // モーダルの世代番号: 開くたびに進める。in-flight の fetch (読み込み・
    // 保存・削除) の応答は、発行時の世代と一致するときだけ state に適用する —
    // A の保存中に閉じて B (または同じ A) で開き直したとき、古い応答が
    // 新しいフォームを上書きしない (Codex 三巡目)。
    const generationRef = useRef(0);

    useEffect(() => {
        if (isOpen && personaId) {
            generationRef.current += 1;
            setLoadedPersonaId(null);
            setIsEditing(false);
            setErrorMessage(null);
            setSavedMessage(null);
            loadAll();
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isOpen, personaId]);

    const loadAll = async () => {
        setIsLoading(true);
        const targetPersonaId = personaIdRef.current;
        const generation = generationRef.current;
        const isStale = () =>
            targetPersonaId !== personaIdRef.current
            || generation !== generationRef.current;
        try {
            const [tplRes, kindsRes, facRes] = await Promise.all([
                fetch(`/api/people/${targetPersonaId}/timetable-template`),
                fetch('/api/config/slot-kinds'),
                fetch(`/api/people/${targetPersonaId}/timetable-template/facilities`),
            ]);
            if (isStale()) return;
            if (!tplRes.ok || !kindsRes.ok || !facRes.ok) {
                console.error('[TimetableTemplateModal] failed to load',
                    tplRes.status, kindsRes.status, facRes.status);
                setErrorMessage('読み込みに失敗しました。モーダルを開き直してください。');
                return;
            }
            const tpl = await tplRes.json();
            const kindList: SlotKindInfo[] = await kindsRes.json();
            const facList: FacilityOption[] = await facRes.json();
            if (isStale()) return;
            setKinds(Array.isArray(kindList) ? kindList : []);
            setFacilities(Array.isArray(facList) ? facList : []);
            if (tpl && Array.isArray(tpl.slots) && tpl.slots.length > 0) {
                setHasTemplate(true);
                setIsEditing(true);
                setRows(tpl.slots.map(toRow));
                setEnabled(tpl.enabled !== false);
            } else {
                setHasTemplate(false);
                setRows([]);
                setEnabled(true);
            }
            setLoadedPersonaId(targetPersonaId);
        } catch (e) {
            console.error(e);
            if (!isStale()) setErrorMessage('サーバーとの通信に失敗しました。');
        } finally {
            if (!isStale()) setIsLoading(false);
        }
    };

    if (!isOpen) return null;

    const updateRow = (index: number, patch: Partial<SlotRow>) => {
        setRows(prev => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
        setSavedMessage(null);
    };

    const addRow = () => {
        setRows(prev => [...prev, {
            start: nextStart(prev), kind: MORNING_CHOICE, title: '',
            facility: MORNING_CHOICE, note: '', budget: '', ref: null,
        }]);
        setSavedMessage(null);
    };

    const removeRow = (index: number) => {
        setRows(prev => prev.filter((_, i) => i !== index));
        setSavedMessage(null);
    };

    const moveRow = (index: number, delta: -1 | 1) => {
        setRows(prev => {
            const target = index + delta;
            if (target < 0 || target >= prev.length) return prev;
            const next = [...prev];
            [next[index], next[target]] = [next[target], next[index]];
            return next;
        });
        setSavedMessage(null);
    };

    const startCreating = () => {
        setIsEditing(true);
        setRows([{
            start: '08:00', kind: MORNING_CHOICE, title: '',
            facility: MORNING_CHOICE, note: '', budget: '', ref: null,
        }]);
        setEnabled(true);
    };

    const handleSave = async () => {
        if (isLoading || !loadedPersonaId || loadedPersonaId !== personaId) {
            alert('読み込みが完了していないため保存できません。モーダルを開き直してください。');
            return;
        }
        if (rows.length === 0) {
            setErrorMessage('コマがありません。1 つ以上のコマを追加してください。');
            return;
        }
        for (const row of rows) {
            if (!/^([01]\d|2[0-3]):([0-5]\d)$/.test(row.start)) {
                setErrorMessage('開始時刻が入っていないコマがあります。時刻 (HH:MM) を指定してください。');
                return;
            }
        }
        setIsSaving(true);
        setErrorMessage(null);
        setSavedMessage(null);
        const targetPersonaId = personaId;
        const generation = generationRef.current;
        const isStale = () =>
            targetPersonaId !== personaIdRef.current
            || generation !== generationRef.current;
        try {
            const res = await fetch(`/api/people/${targetPersonaId}/timetable-template`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slots: rows.map(toPayloadSlot), enabled }),
            });
            if (isStale()) return;
            if (res.ok) {
                const saved = await res.json();
                if (isStale()) return;
                setHasTemplate(true);
                setRows((saved.slots || []).map(toRow));
                setEnabled(saved.enabled !== false);
                setSavedMessage('保存しました。次の朝からこのテンプレートで一日が始まります。');
            } else {
                const err = await res.json().catch(() => ({}));
                if (isStale()) return;
                const detail = typeof err.detail === 'string'
                    ? err.detail
                    : JSON.stringify(err.detail ?? err);
                setErrorMessage(`保存できませんでした: ${detail}`);
            }
        } catch (e) {
            console.error(e);
            if (!isStale()) setErrorMessage('サーバーとの通信に失敗しました。');
        } finally {
            // isSaving は世代を跨いでも必ず畳む (spinner の出しっぱなし防止)
            setIsSaving(false);
        }
    };

    const handleDelete = async () => {
        if (!hasTemplate) return;
        if (!confirm('テンプレートを削除しますか？\n削除すると毎朝の全生成（ペルソナが一から時間割を組む形）に戻ります。')) return;
        setIsDeleting(true);
        setErrorMessage(null);
        setSavedMessage(null);
        const targetPersonaId = personaId;
        const generation = generationRef.current;
        const isStale = () =>
            targetPersonaId !== personaIdRef.current
            || generation !== generationRef.current;
        try {
            const res = await fetch(`/api/people/${targetPersonaId}/timetable-template`, { method: 'DELETE' });
            if (isStale()) return;
            if (res.ok) {
                setHasTemplate(false);
                setIsEditing(false);
                setRows([]);
            } else {
                const err = await res.json().catch(() => ({}));
                if (isStale()) return;
                setErrorMessage(`削除に失敗しました: ${err.detail || res.status}`);
            }
        } catch (e) {
            console.error(e);
            if (!isStale()) setErrorMessage('サーバーとの通信に失敗しました。');
        } finally {
            setIsDeleting(false);
        }
    };

    // 保存済みの値がいまの選択肢に無い場合 (施設の撤去・種別の除去など) も、
    // 値を勝手に書き換えず「今は選べない」ラベル付きで表示する (fail-open 表示)。
    const kindOptionMissing = (value: string) =>
        value !== MORNING_CHOICE && !kinds.some(k => k.name === value);
    const facilityOptionMissing = (value: string) =>
        value !== MORNING_CHOICE && !facilities.some(f => f.id === value);

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>
                        <CalendarClock size={20} /> 習慣テンプレート{personaName ? `: ${personaName}` : ''}
                    </h2>
                    <button className={styles.closeButton} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {isLoading ? (
                        <div className={styles.loadingWrap}>
                            <Loader2 className="spin" size={32} />
                        </div>
                    ) : !isEditing ? (
                        <>
                            <div className={styles.description}>
                                いまは毎朝、{personaName || 'このペルソナ'}が一から時間割を組みます。
                                テンプレートを作ると、決まった習慣の枠に沿って暮らすようになります。
                                枠の一部を「{MORNING_LABEL}」にしておくと、そこだけは本人が朝に選びます。
                            </div>
                            {errorMessage && <div className={styles.errorBox}>{errorMessage}</div>}
                            <button className={styles.createBtn} onClick={startCreating} disabled={!loadedPersonaId}>
                                <Plus size={16} /> テンプレートを作る
                            </button>
                        </>
                    ) : (
                        <>
                            <div className={styles.description}>
                                一日のコマ (時間の区切り) を上から順に並べます。決めておきたい項目は入力し、
                                本人に任せたい項目は「{MORNING_LABEL}」のままにしておくと、毎朝そこだけ自分で決めます。
                            </div>

                            <label className={styles.enabledRow}>
                                <input
                                    type="checkbox"
                                    checked={enabled}
                                    onChange={e => { setEnabled(e.target.checked); setSavedMessage(null); }}
                                />
                                <span>このテンプレートを使う（オフの間は毎朝の全生成に戻ります）</span>
                            </label>

                            <div className={styles.slotList}>
                                {rows.map((row, i) => (
                                    <div key={i} className={styles.slotCard}>
                                        <div className={styles.slotRow}>
                                            <div className={styles.fieldStart}>
                                                <label className={styles.fieldLabel}>開始</label>
                                                <input
                                                    className={styles.input}
                                                    type="time"
                                                    value={row.start}
                                                    onChange={e => updateRow(i, { start: e.target.value })}
                                                />
                                            </div>
                                            <div className={styles.fieldKind}>
                                                <label className={styles.fieldLabel}>すること（種別）</label>
                                                <select
                                                    className={styles.select}
                                                    value={row.kind}
                                                    onChange={e => updateRow(i, { kind: e.target.value })}
                                                >
                                                    <option value={MORNING_CHOICE}>{MORNING_LABEL}</option>
                                                    {kinds.map(k => (
                                                        <option key={k.id} value={k.name} title={k.description}>{k.name}</option>
                                                    ))}
                                                    {kindOptionMissing(row.kind) && (
                                                        <option value={row.kind}>{row.kind}（今は選べない種別）</option>
                                                    )}
                                                </select>
                                            </div>
                                            <div className={styles.fieldFacility}>
                                                <label className={styles.fieldLabel}>場所</label>
                                                <select
                                                    className={styles.select}
                                                    value={row.facility}
                                                    onChange={e => updateRow(i, { facility: e.target.value })}
                                                >
                                                    <option value={MORNING_CHOICE}>{MORNING_LABEL}</option>
                                                    {facilities.map(f => (
                                                        <option key={f.id} value={f.id}>{f.name}</option>
                                                    ))}
                                                    {facilityOptionMissing(row.facility) && (
                                                        <option value={row.facility}>{row.facility}（今は行けない場所）</option>
                                                    )}
                                                </select>
                                            </div>
                                            <div className={styles.rowButtons}>
                                                <button
                                                    className={styles.iconBtn} title="上へ"
                                                    onClick={() => moveRow(i, -1)} disabled={i === 0}
                                                ><ArrowUp size={14} /></button>
                                                <button
                                                    className={styles.iconBtn} title="下へ"
                                                    onClick={() => moveRow(i, 1)} disabled={i === rows.length - 1}
                                                ><ArrowDown size={14} /></button>
                                                <button
                                                    className={`${styles.iconBtn} ${styles.deleteIconBtn}`} title="このコマを削除"
                                                    onClick={() => removeRow(i)}
                                                ><Trash2 size={14} /></button>
                                            </div>
                                        </div>
                                        <div className={styles.slotRow}>
                                            <div className={styles.fieldTitle}>
                                                <label className={styles.fieldLabel}>見出し</label>
                                                <input
                                                    className={styles.input}
                                                    type="text"
                                                    value={row.title}
                                                    placeholder={MORNING_LABEL}
                                                    onChange={e => updateRow(i, { title: e.target.value })}
                                                />
                                            </div>
                                            <div className={styles.fieldNote}>
                                                <label className={styles.fieldLabel}>方針（どういう内容か。曖昧でよい）</label>
                                                <input
                                                    className={styles.input}
                                                    type="text"
                                                    value={row.note}
                                                    placeholder={MORNING_LABEL}
                                                    onChange={e => updateRow(i, { note: e.target.value })}
                                                />
                                            </div>
                                            <div className={styles.fieldBudget}>
                                                <label className={styles.fieldLabel}>作業回数の予算</label>
                                                <input
                                                    className={styles.input}
                                                    type="number"
                                                    min={0}
                                                    value={row.budget}
                                                    placeholder={MORNING_LABEL}
                                                    onChange={e => updateRow(i, { budget: e.target.value })}
                                                />
                                            </div>
                                        </div>
                                    </div>
                                ))}
                            </div>

                            <button className={styles.addBtn} onClick={addRow}>
                                <Plus size={16} /> コマを追加
                            </button>

                            <div className={styles.hint}>
                                コマは一日の流れ順（起床時刻から始まる順）に上から並べてください。順番が
                                合っていない場合は保存時にどのコマが問題かをお知らせします。
                            </div>

                            {errorMessage && <div className={styles.errorBox}>{errorMessage}</div>}
                            {savedMessage && <div className={styles.savedBox}>{savedMessage}</div>}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    {isEditing && hasTemplate && (
                        <button className={styles.deleteBtn} onClick={handleDelete} disabled={isDeleting || isSaving}>
                            {isDeleting ? <Loader2 size={16} className="spin" /> : <Trash2 size={16} />}
                            テンプレートを削除
                        </button>
                    )}
                    <div className={styles.footerRight}>
                        <button className={styles.cancelBtn} onClick={onClose}>閉じる</button>
                        {isEditing && (
                            <button
                                className={styles.saveBtn}
                                onClick={handleSave}
                                disabled={isLoading || isSaving || isDeleting || !loadedPersonaId || loadedPersonaId !== personaId}
                            >
                                {isSaving ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                                保存
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
