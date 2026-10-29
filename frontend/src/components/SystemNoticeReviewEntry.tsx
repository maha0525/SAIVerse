"use client";
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

import { useEffect, useState } from "react";
import SystemNoticeReviewModal, {
    NOTICE_REVIEW_ALERT_ID,
    NOTICE_REVIEW_CHANGED_EVENT,
} from "./SystemNoticeReviewModal";

// 確認待ちの行を確定する画面への常設の入口 (設定の「便利機能」に置く)。画面上部の
// バナーは閉じられるので、閉じた後もここから同じ画面を開ける。出すかどうかは
// バナーと同じく、サーバーが置く確認待ちの警告 (NOTICE_REVIEW_ALERT_ID) の有無で
// 決める — 確認待ちの行があるか、記憶を読めなかったペルソナがある間だけ出る。
// 確定の操作の後は NOTICE_REVIEW_CHANGED_EVENT を出し、バナーとこの入口の両方が
// それを聞いて警告を取り直す (どちらから開いて確定しても、もう一方が古い警告を
// 出し続けない)。

interface ReviewAlert {
    id: string;
    title: string;
    message: string;
}

interface Props {
    /** 置き場 (設定の画面) の見た目に合わせるためのクラス名 */
    classNames?: { card?: string; title?: string; desc?: string; button?: string };
}

export default function SystemNoticeReviewEntry({ classNames = {} }: Props) {
    useLocale();
    const [alert, setAlert] = useState<ReviewAlert | null>(null);
    const [open, setOpen] = useState(false);

    const fetchAlert = async () => {
        try {
            const res = await apiFetch("/api/system/alerts");
            if (!res.ok) return;
            const data: { alerts?: ReviewAlert[] } = await res.json();
            setAlert((data.alerts || []).find((a) => a.id === NOTICE_REVIEW_ALERT_ID) || null);
        } catch {
            // 取れなければ入口を出さない (バナーの側と同じく、次の読み込みで出る)
        }
    };

    useEffect(() => {
        fetchAlert();
        window.addEventListener(NOTICE_REVIEW_CHANGED_EVENT, fetchAlert);
        return () => window.removeEventListener(NOTICE_REVIEW_CHANGED_EVENT, fetchAlert);
    }, []);

    const announceChanged = () => {
        window.dispatchEvent(new Event(NOTICE_REVIEW_CHANGED_EVENT));
    };

    const modal = open ? (
        <SystemNoticeReviewModal onClose={() => setOpen(false)} onChanged={announceChanged} />
    ) : null;
    if (!alert) return modal;

    return (
        <>
            <div className={classNames.card}>
                <h4 className={classNames.title}>{uiText("components.SystemNoticeReviewEntry.title")}</h4>
                <p className={classNames.desc}>{alert.title}</p>
                <button
                    type="button"
                    className={classNames.button}
                    onClick={() => setOpen(true)}
                >
                    {uiText("components.SystemNoticeReviewEntry.open")}
                </button>
            </div>
            {modal}
        </>
    );
}
