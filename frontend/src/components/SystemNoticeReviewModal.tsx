"use client";
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import ModalOverlay from "./common/ModalOverlay";
import styles from "./SystemNoticeReviewModal.module.css";

// 確認待ちの行 (起動時の移行がシステム通知か発話かを決められなかった記憶の行) を
// 所有者が確定する画面。docs/intent/system_notice_delivery.md「確認待ちの行の確定」。
// 選べる操作は行ごとにサーバーが決めて返す (この画面は本文を見て判定しない)。
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
    tags: string[];
    content: string;
    reason: string;
    created_at: number | null;
    actions: ReviewAction[];
    version: string;
    attachments: { kind: AttachmentKind; ref: string }[];
    attachment_counts: Partial<Record<AttachmentKind, number>>;
    /** 「正式に変換」が、案内文の通知と音声だけのユーザーの発言の二行に分ける操作になる */
    system_split?: boolean;
}

interface ReviewGroup {
    group_id: string;
    role: string;
    head: string;
    tags: string[];
    reason: string;
    actions: ReviewAction[];
    /** 束の「正式に変換」が二行に分ける操作になる (ReviewRow.system_split と同じ) */
    system_split?: boolean;
    count: number;
    personas: { persona_id: string; persona_name: string; count: number }[];
    rows: ReviewRow[];
}

interface ReviewListing {
    total: number;
    groups: ReviewGroup[];
    errors: { persona_id: string; persona_name?: string; error: string }[];
}

interface ApplyResult {
    applied: { persona_id: string; message_id: string; result: string }[];
    rejected: { persona_id: string; message_id: string; reason: string }[];
    remaining: number;
}

interface Props {
    onClose: () => void;
    /** 確定の操作のあと、警告を取り直すために呼ぶ (開いた側は NOTICE_REVIEW_CHANGED_EVENT を出す) */
    onChanged: () => void | Promise<void>;
}

const ATTACHMENT_KIND_ORDER: AttachmentKind[] = ["image", "document", "audio", "video", "other"];

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
    const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());

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
        // 束ごとの一括の操作は、どの操作でも対象の件数を見せて確かめる。
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

    const toggleGroup = (id: string) => {
        setOpenGroups((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
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
                    {listing && listing.groups.map((group) => {
                        const open = openGroups.has(group.group_id);
                        const example = group.rows[0];
                        const blockedByAttachments = !group.actions.includes("system")
                            && group.rows.some((r) => (r.attachments || []).length > 0);
                        return (
                            <section key={group.group_id} className={styles.group}>
                                <div className={styles.groupHeader}>
                                    <span className={styles.count}>
                                        {uiText("components.SystemNoticeReviewModal.groupCount", { count: group.count })}
                                    </span>
                                    <span className={styles.role}>{roleLabel(group.role)}</span>
                                    <span className={styles.personas}>
                                        {group.personas
                                            .map((p) => (group.personas.length > 1 ? `${p.persona_name} (${p.count})` : p.persona_name))
                                            .join(", ")}
                                    </span>
                                </div>
                                {example && <pre className={styles.content}>{example.content}</pre>}
                                {example && attachmentBlock(example)}
                                {group.tags.length > 0 && (
                                    <div className={styles.tags}>{group.tags.join(" ")}</div>
                                )}
                                {blockedByAttachments && (
                                    <p className={styles.muted}>
                                        {uiText("components.SystemNoticeReviewModal.attachmentsNoSystem")}
                                    </p>
                                )}
                                {group.system_split && (
                                    <p className={styles.muted}>
                                        {uiText("components.SystemNoticeReviewModal.systemSplitNote")}
                                    </p>
                                )}
                                <div className={styles.actions}>
                                    <span className={styles.actionsLabel}>
                                        {uiText("components.SystemNoticeReviewModal.applyAll", { count: group.count })}
                                    </span>
                                    {group.actions.map((action) => actionButton(action, group.rows, action))}
                                </div>
                                <button
                                    type="button"
                                    className={styles.expandButton}
                                    onClick={() => toggleGroup(group.group_id)}
                                    aria-expanded={open}
                                >
                                    {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                    {uiText("components.SystemNoticeReviewModal.chooseEach", { count: group.count })}
                                </button>
                                {open && (
                                    <ul className={styles.rows}>
                                        {group.rows.map((row) => (
                                            <li key={`${row.persona_id}/${row.message_id}`} className={styles.row}>
                                                <div className={styles.rowMeta}>
                                                    <span>{row.persona_name}</span>
                                                    <span className={styles.role}>{roleLabel(row.role)}</span>
                                                    {row.created_at ? (
                                                        <span className={styles.muted}>
                                                            {new Date(row.created_at * 1000).toLocaleString()}
                                                        </span>
                                                    ) : null}
                                                </div>
                                                <pre className={styles.content}>{row.content}</pre>
                                                {attachmentBlock(row)}
                                                <div className={styles.actions}>
                                                    {row.actions.map((action) => actionButton(action, [row], action))}
                                                </div>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </section>
                        );
                    })}
                </div>
            </div>
        </ModalOverlay>
    );
}
