"use client";
import { apiFetch } from '@/i18n/api';

import { getFormatLocale, t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import ModalOverlay from "./common/ModalOverlay";
import styles from "./SystemNoticeReviewModal.module.css";

// 確認待ちの行 (起動時の移行がシステム通知か発話かを決められなかった記憶の行) を
// 所有者が確定する画面。docs/intent/system_notice_delivery.md「確認待ちの行の確定」。
// 一覧はペルソナごと、その中の role ごとに並ぶ (saiverse/system_notice_review.py の
// build_review_listing)。role で分けるのは role によって選べる操作が変わるため。
// 確認待ちの理由・タグ・line_role は所有者の選択に関わらないので出さない。
// role の中は行を一件ずつ、日時・全文・添付で並べ、行ごとに前後の文脈を開ける
// (GET /api/system/notice-review/context)。
// 選べる操作は行ごとにサーバーが決めて返し、role ごとの一括の操作は全行の共通部分を
// サーバーが返す (この画面は本文を見て判定しない)。
// 各行は全文・添付・行のハッシュ (version) を持つ。確定の操作には画面が見たハッシュを送り、
// サーバーはその後に中身が変わった行を書き換えずに拒否する (reason=changed)。

/** 確認待ちの行があるあいだサーバーが置く警告の id (saiverse/system_notice_review.py
 * の REVIEW_ALERT_ID)。画面上部のバナーと設定の「便利機能」の入口が、この警告の
 * 有無で確定の画面への入口を出す。 */
export const NOTICE_REVIEW_ALERT_ID = "system_notice_review_pending";

/** 確定の操作の後に window へ出すイベントの名前。画面上部のバナー (SystemAlertBanner)
 * と設定の「便利機能」の入口 (SystemNoticeReviewEntry) は警告をそれぞれ自分の状態に
 * 持つので、どちらから確定しても両方がこれを聞いて /api/system/alerts を取り直す。
 * 出すのは確定の画面を開いた側 (onChanged)。 */
export const NOTICE_REVIEW_CHANGED_EVENT = "saiverse:system-notice-review-changed";

type ReviewAction = "keep" | "system" | "user" | "delete";
type AttachmentKind = "image" | "document" | "audio" | "video" | "other";

interface ReviewRow {
    persona_id: string;
    persona_name: string;
    message_id: string;
    role: string;
    content: string;
    created_at: number | null;
    actions: ReviewAction[];
    version: string;
    attachments: { kind: AttachmentKind; ref: string }[];
    attachment_counts: Partial<Record<AttachmentKind, number>>;
    /** 「正式に変換」が、案内文の通知と音声だけのユーザーの発言の二行に分ける操作になる */
    system_split?: boolean;
}

/** 一人のペルソナの、同じ role の行 */
interface ReviewKind {
    kind_id: string;
    role: string;
    count: number;
    /** この並びの全部の行に一括で当てられる操作 (行ごとの actions の共通部分) */
    actions: ReviewAction[];
    /** 一括の「正式に変換」が二行に分ける操作になる (全行が ReviewRow.system_split) */
    system_split?: boolean;
    rows: ReviewRow[];
}

interface ReviewPersona {
    persona_id: string;
    persona_name: string;
    count: number;
    kinds: ReviewKind[];
}

interface ReviewListing {
    total: number;
    personas: ReviewPersona[];
    errors: { persona_id: string; persona_name?: string; error: string }[];
}

interface ApplyResult {
    applied: { persona_id: string; message_id: string; result: string }[];
    rejected: { persona_id: string; message_id: string; reason: string }[];
    remaining: number;
}

/** 前後の文脈の一行 (saiverse/system_notice_review.py の build_review_context) */
interface ContextMessage {
    message_id: string;
    role: string;
    content: string;
    created_at: number | null;
    /** 確認待ちの対象の行 */
    target: boolean;
}

type ContextState =
    | { status: "loading" }
    | { status: "loaded"; messages: ContextMessage[] }
    | { status: "failed" };

interface Props {
    onClose: () => void;
    /** 確定の操作のあと、警告を取り直すために呼ぶ (開いた側は NOTICE_REVIEW_CHANGED_EVENT を出す) */
    onChanged: () => void | Promise<void>;
}

const ATTACHMENT_KIND_ORDER: AttachmentKind[] = ["image", "document", "audio", "video", "other"];

/** role ごとに最初に出す行の数。残りは「さらに N 件を表示」で開く。 */
export const ROWS_SHOWN = 5;

function hasAttachments(row: ReviewRow): boolean {
    return (row.attachments || []).length > 0;
}

/** 添付があるために「正式に変換」を選べない行 (二行分割になる行は選べるので入らない) */
function blockedByAttachments(row: ReviewRow): boolean {
    return hasAttachments(row) && !row.actions.includes("system");
}

function rowKey(row: ReviewRow): string {
    return `${row.persona_id}/${row.message_id}`;
}

/** created_at (秒) を利用者の地方時で年月日と時刻まで。無ければ「日時不明」。 */
function formatTime(createdAt: number | null): string {
    if (createdAt === null || createdAt === undefined) {
        return uiText("components.SystemNoticeReviewModal.unknownTime");
    }
    return new Date(createdAt * 1000).toLocaleString(getFormatLocale(), {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
}

function actionLabel(action: ReviewAction): string {
    switch (action) {
        case "keep": return uiText("components.SystemNoticeReviewModal.actionKeep");
        case "system": return uiText("components.SystemNoticeReviewModal.actionSystem");
        case "user": return uiText("components.SystemNoticeReviewModal.actionUser");
        case "delete": return uiText("components.SystemNoticeReviewModal.actionDelete");
    }
}

function roleLabel(role: string): string {
    const r = role.toLowerCase();
    if (r === "user") return uiText("components.SystemNoticeReviewModal.roleUser");
    if (r === "assistant" || r === "model") return uiText("components.SystemNoticeReviewModal.rolePersona");
    if (r === "system") return uiText("components.SystemNoticeReviewModal.roleSystem");
    return role;
}

function kindLabel(kind: AttachmentKind, count: number): string {
    switch (kind) {
        case "image": return uiText("components.SystemNoticeReviewModal.kindImage", { count });
        case "document": return uiText("components.SystemNoticeReviewModal.kindDocument", { count });
        case "audio": return uiText("components.SystemNoticeReviewModal.kindAudio", { count });
        case "video": return uiText("components.SystemNoticeReviewModal.kindVideo", { count });
        default: return uiText("components.SystemNoticeReviewModal.kindOther", { count });
    }
}

function attachmentSummary(row: ReviewRow): string {
    const counts = row.attachment_counts || {};
    return ATTACHMENT_KIND_ORDER
        .filter((kind) => (counts[kind] || 0) > 0)
        .map((kind) => kindLabel(kind, counts[kind] || 0))
        .join(", ");
}

export default function SystemNoticeReviewModal({ onClose, onChanged }: Props) {
    useLocale();
    const [listing, setListing] = useState<ReviewListing | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    // ROWS_SHOWN 件より後ろの行も出している並びの kind_id
    const [expandedKinds, setExpandedKinds] = useState<Set<string>>(new Set());
    // 前後の文脈を開いている行 (rowKey) と、取りに行った結果
    const [openContexts, setOpenContexts] = useState<Set<string>>(new Set());
    const [contexts, setContexts] = useState<Record<string, ContextState>>({});

    const load = async (): Promise<ReviewListing | null> => {
        try {
            const res = await apiFetch("/api/system/notice-review");
            if (!res.ok) {
                setLoadError(uiText("components.SystemNoticeReviewModal.loadFailed", { status: res.status }));
                return null;
            }
            const data: ReviewListing = await res.json();
            setListing(data);
            setLoadError(null);
            return data;
        } catch {
            setLoadError(uiText("components.SystemNoticeReviewModal.networkError"));
            return null;
        }
    };

    useEffect(() => {
        load();
    }, []);

    const apply = async (action: ReviewAction, rows: ReviewRow[]) => {
        if (rows.length === 0 || busy) return;
        // role ごとの一括の操作は、どの操作でも対象の件数を見せて確かめる。
        // 一件ずつの操作は、元に戻せない削除だけ確かめる。
        let question: string | null = null;
        if (action === "delete") {
            question = rows.length === 1
                ? uiText("components.SystemNoticeReviewModal.confirmDeleteOne")
                : uiText("components.SystemNoticeReviewModal.confirmDeleteMany", { count: rows.length });
        } else if (rows.length > 1) {
            question = uiText("components.SystemNoticeReviewModal.confirmApplyMany", {
                count: rows.length, action: actionLabel(action),
            });
        }
        if (question !== null && !window.confirm(question)) return;
        setBusy(true);
        try {
            // 送るのは行の識別 (ペルソナと行の id) と、画面が見た行のハッシュ。どの行に何を
            // 当ててよいか・見た内容のままかは、サーバーが行ごとに確かめ直す。
            const res = await apiFetch("/api/system/notice-review/apply", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action,
                    items: rows.map((r) => ({
                        persona_id: r.persona_id, message_id: r.message_id, version: r.version,
                    })),
                }),
            });
            if (!res.ok) {
                window.alert(uiText("components.SystemNoticeReviewModal.applyFailed", { status: res.status }));
                return;
            }
            const result: ApplyResult = await res.json();
            const changed = result.rejected.filter((r) => r.reason === "changed").length;
            const otherRejected = result.rejected.length - changed;
            if (changed > 0) {
                window.alert(uiText("components.SystemNoticeReviewModal.rejectedChanged", { count: changed }));
            }
            if (otherRejected > 0) {
                window.alert(uiText("components.SystemNoticeReviewModal.rejected", { count: otherRejected }));
            }
            await onChanged();
            // 確定で記憶が変わったので、開いていた前後の文脈 (確定の前に取ったもの) は閉じる
            setOpenContexts(new Set());
            const next = await load();
            // 読めなかったペルソナが残っているあいだは閉じない (確認待ちの行が
            // そこに残っているかもしれないので、読み直しの手段を見せ続ける)。
            if (next && next.total === 0 && next.errors.length === 0) onClose();
        } catch {
            window.alert(uiText("components.SystemNoticeReviewModal.networkError"));
        } finally {
            setBusy(false);
        }
    };

    const retry = async () => {
        if (busy) return;
        setBusy(true);
        try {
            await load();
            await onChanged();
        } finally {
            setBusy(false);
        }
    };

    const toggleKind = (id: string) => {
        setExpandedKinds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    // 前後の文脈は、開くたびに取りに行く (閉じている行のためには取りに行かない)。
    const toggleContext = async (row: ReviewRow) => {
        const key = rowKey(row);
        if (openContexts.has(key)) {
            setOpenContexts((prev) => {
                const next = new Set(prev);
                next.delete(key);
                return next;
            });
            return;
        }
        setOpenContexts((prev) => new Set(prev).add(key));
        setContexts((prev) => ({ ...prev, [key]: { status: "loading" } }));
        let state: ContextState;
        try {
            const query = new URLSearchParams({ persona_id: row.persona_id, message_id: row.message_id });
            const res = await apiFetch(`/api/system/notice-review/context?${query.toString()}`);
            if (res.ok) {
                const data: { messages: ContextMessage[] } = await res.json();
                state = { status: "loaded", messages: data.messages || [] };
            } else {
                state = { status: "failed" };
            }
        } catch {
            state = { status: "failed" };
        }
        setContexts((prev) => ({ ...prev, [key]: state }));
    };

    const actionButton = (action: ReviewAction, rows: ReviewRow[], key: string) => (
        <button
            key={key}
            type="button"
            className={`${styles.actionButton} ${action === "delete" ? styles.danger : ""}`}
            onClick={() => apply(action, rows)}
            disabled={busy}
        >
            {actionLabel(action)}
        </button>
    );

    const attachmentBlock = (row: ReviewRow) => {
        if (!row.attachments || row.attachments.length === 0) return null;
        return (
            <div className={styles.attachments}>
                <span>
                    {uiText("components.SystemNoticeReviewModal.attachments", { list: attachmentSummary(row) })}
                </span>
                <ul className={styles.attachmentRefs}>
                    {row.attachments.map((item, i) => (
                        <li key={`${i}:${item.ref}`}>{item.ref}</li>
                    ))}
                </ul>
            </div>
        );
    };

    const contextBlock = (row: ReviewRow) => {
        const key = rowKey(row);
        if (!openContexts.has(key)) return null;
        const state = contexts[key];
        if (!state || state.status === "loading") {
            return <p className={styles.muted}>{uiText("components.SystemNoticeReviewModal.contextLoading")}</p>;
        }
        if (state.status === "failed") {
            return <p className={styles.error}>{uiText("components.SystemNoticeReviewModal.contextFailed")}</p>;
        }
        return (
            <ol className={styles.context}>
                {state.messages.map((msg) => (
                    <li
                        key={msg.message_id}
                        className={`${styles.contextItem} ${msg.target ? styles.contextTarget : ""}`}
                    >
                        <div className={styles.contextMeta}>
                            <span className={styles.contextRole}>{roleLabel(msg.role)}</span>
                            <span>{formatTime(msg.created_at)}</span>
                        </div>
                        <pre className={styles.contextContent}>{msg.content}</pre>
                    </li>
                ))}
            </ol>
        );
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlayAboveBanner}>
            <div className={styles.modal} role="dialog" aria-modal="true">
                <div className={styles.header}>
                    <h2 className={styles.question}>{uiText("components.SystemNoticeReviewModal.question")}</h2>
                    <button
                        type="button"
                        className={styles.closeButton}
                        onClick={onClose}
                        title={uiText("components.SystemNoticeReviewModal.close")}
                        aria-label={uiText("components.SystemNoticeReviewModal.close")}
                    >
                        <X size={20} />
                    </button>
                </div>
                <div className={styles.body}>
                    <p className={styles.note}>{uiText("components.SystemNoticeReviewModal.note")}</p>
                    {loadError && <p className={styles.error}>{loadError}</p>}
                    {!listing && !loadError && (
                        <p className={styles.muted}>{uiText("components.SystemNoticeReviewModal.loading")}</p>
                    )}
                    {listing && listing.errors.length > 0 && (
                        <div className={styles.errorBox}>
                            <p className={styles.error}>
                                {uiText("components.SystemNoticeReviewModal.readErrors", {
                                    names: listing.errors.map((e) => e.persona_name || e.persona_id).join(", "),
                                })}
                            </p>
                            <button
                                type="button"
                                className={styles.actionButton}
                                onClick={retry}
                                disabled={busy}
                            >
                                {uiText("components.SystemNoticeReviewModal.retry")}
                            </button>
                        </div>
                    )}
                    {listing && listing.total === 0 && listing.errors.length === 0 && (
                        <p className={styles.muted}>{uiText("components.SystemNoticeReviewModal.empty")}</p>
                    )}
                    {listing && listing.personas.map((persona) => (
                        <section key={persona.persona_id} className={styles.persona}>
                            <h3 className={styles.personaHeader}>
                                {uiText("components.SystemNoticeReviewModal.personaHeader", {
                                    name: persona.persona_name, count: persona.count,
                                })}
                            </h3>
                            {persona.kinds.map((kind) => {
                                const expanded = expandedKinds.has(kind.kind_id);
                                const shown = expanded ? kind.rows : kind.rows.slice(0, ROWS_SHOWN);
                                const hidden = kind.rows.length - ROWS_SHOWN;
                                // 全行に当てはまる説明は並びに一度だけ出し、一部の行だけなら
                                // その行に出す (同じ文を行の数だけ並べない)。
                                const allBlocked = kind.rows.length > 0 && kind.rows.every(blockedByAttachments);
                                const allSplit = Boolean(kind.system_split);
                                // 一括の操作に無い操作を選べる行がある (行のボタンからは選べる)
                                const bulkSubset = kind.rows.some(
                                    (r) => r.actions.some((a) => !kind.actions.includes(a)),
                                );
                                return (
                                    <div key={kind.kind_id} className={styles.kind}>
                                        <div className={styles.kindHeader}>
                                            <span className={styles.kindTitle}>
                                                {uiText("components.SystemNoticeReviewModal.kindTitle", { role: roleLabel(kind.role) })}
                                            </span>
                                            <span className={styles.count}>
                                                {uiText("components.SystemNoticeReviewModal.groupCount", { count: kind.count })}
                                            </span>
                                        </div>
                                        {allBlocked && (
                                            <p className={styles.muted}>
                                                {uiText("components.SystemNoticeReviewModal.attachmentsNoSystem")}
                                            </p>
                                        )}
                                        {allSplit && (
                                            <p className={styles.muted}>
                                                {uiText("components.SystemNoticeReviewModal.systemSplitNote")}
                                            </p>
                                        )}
                                        {kind.count > 1 && (
                                            <div className={`${styles.actions} ${styles.bulk}`}>
                                                <span className={styles.actionsLabel}>
                                                    {uiText("components.SystemNoticeReviewModal.applyAll", { count: kind.count })}
                                                </span>
                                                {kind.actions.map((action) => actionButton(action, kind.rows, action))}
                                            </div>
                                        )}
                                        {kind.count > 1 && bulkSubset && (
                                            <p className={styles.muted}>
                                                {uiText("components.SystemNoticeReviewModal.bulkSubsetNote")}
                                            </p>
                                        )}
                                        <ul className={styles.rows}>
                                            {shown.map((row) => {
                                                const contextOpen = openContexts.has(rowKey(row));
                                                return (
                                                    <li key={rowKey(row)} className={styles.row}>
                                                        <div className={styles.rowMeta}>
                                                            <span className={styles.rowTime}>{formatTime(row.created_at)}</span>
                                                            <button
                                                                type="button"
                                                                className={styles.contextButton}
                                                                onClick={() => toggleContext(row)}
                                                                aria-expanded={contextOpen}
                                                            >
                                                                {contextOpen
                                                                    ? uiText("components.SystemNoticeReviewModal.contextHide")
                                                                    : uiText("components.SystemNoticeReviewModal.contextShow")}
                                                            </button>
                                                        </div>
                                                        <pre className={styles.content}>{row.content}</pre>
                                                        {attachmentBlock(row)}
                                                        {!allBlocked && blockedByAttachments(row) && (
                                                            <p className={styles.muted}>
                                                                {uiText("components.SystemNoticeReviewModal.attachmentsNoSystem")}
                                                            </p>
                                                        )}
                                                        {!allSplit && row.system_split && (
                                                            <p className={styles.muted}>
                                                                {uiText("components.SystemNoticeReviewModal.systemSplitNote")}
                                                            </p>
                                                        )}
                                                        <div className={styles.actions}>
                                                            {row.actions.map((action) => actionButton(action, [row], action))}
                                                        </div>
                                                        {contextBlock(row)}
                                                    </li>
                                                );
                                            })}
                                        </ul>
                                        {hidden > 0 && (
                                            <button
                                                type="button"
                                                className={styles.expandButton}
                                                onClick={() => toggleKind(kind.kind_id)}
                                                aria-expanded={expanded}
                                            >
                                                {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                                {expanded
                                                    ? uiText("components.SystemNoticeReviewModal.showLess", { count: ROWS_SHOWN })
                                                    : uiText("components.SystemNoticeReviewModal.showMore", { count: hidden })}
                                            </button>
                                        )}
                                    </div>
                                );
                            })}
                        </section>
                    ))}
                </div>
            </div>
        </ModalOverlay>
    );
}
