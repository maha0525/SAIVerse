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

type ReviewAction = "keep" | "system" | "user" | "delete";

interface ReviewRow {
    persona_id: string;
    persona_name: string;
    message_id: string;
    role: string;
    tags: string[];
    content: string;
    content_truncated: boolean;
    reason: string;
    created_at: number | null;
    actions: ReviewAction[];
}

interface ReviewGroup {
    group_id: string;
    role: string;
    head: string;
    tags: string[];
    reason: string;
    actions: ReviewAction[];
    count: number;
    personas: { persona_id: string; persona_name: string; count: number }[];
    rows: ReviewRow[];
}

interface ReviewListing {
    total: number;
    groups: ReviewGroup[];
    errors: { persona_id: string; error: string }[];
}

interface ApplyResult {
    applied: { persona_id: string; message_id: string; result: string }[];
    rejected: { persona_id: string; message_id: string; reason: string }[];
    remaining: number;
}

interface Props {
    onClose: () => void;
    /** 確定の操作のあと、画面上部の警告を取り直すために呼ぶ */
    onChanged: () => void | Promise<void>;
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
        if (action === "delete") {
            const ok = window.confirm(
                rows.length === 1
                    ? uiText("components.SystemNoticeReviewModal.confirmDeleteOne")
                    : uiText("components.SystemNoticeReviewModal.confirmDeleteMany", { count: rows.length }),
            );
            if (!ok) return;
        }
        setBusy(true);
        try {
            // 送るのは行の識別 (ペルソナと行の id) だけ。どの行に何を当ててよいかは
            // サーバーが行ごとに確かめ直す。
            const res = await apiFetch("/api/system/notice-review/apply", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action,
                    items: rows.map((r) => ({ persona_id: r.persona_id, message_id: r.message_id })),
                }),
            });
            if (!res.ok) {
                window.alert(uiText("components.SystemNoticeReviewModal.applyFailed", { status: res.status }));
                return;
            }
            const result: ApplyResult = await res.json();
            if (result.rejected.length > 0) {
                window.alert(uiText("components.SystemNoticeReviewModal.rejected", { count: result.rejected.length }));
            }
            await onChanged();
            const next = await load();
            if (next && next.total === 0) onClose();
        } catch {
            window.alert(uiText("components.SystemNoticeReviewModal.networkError"));
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
                        <p className={styles.error}>
                            {uiText("components.SystemNoticeReviewModal.readErrors", {
                                names: listing.errors.map((e) => e.persona_id).join(", "),
                            })}
                        </p>
                    )}
                    {listing && listing.total === 0 && (
                        <p className={styles.muted}>{uiText("components.SystemNoticeReviewModal.empty")}</p>
                    )}
                    {listing && listing.groups.map((group) => {
                        const open = openGroups.has(group.group_id);
                        const example = group.rows[0];
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
                                {example && (
                                    <pre className={styles.content}>
                                        {example.content}{example.content_truncated ? "…" : ""}
                                    </pre>
                                )}
                                {group.tags.length > 0 && (
                                    <div className={styles.tags}>{group.tags.join(" ")}</div>
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
                                                <pre className={styles.content}>
                                                    {row.content}{row.content_truncated ? "…" : ""}
                                                </pre>
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
