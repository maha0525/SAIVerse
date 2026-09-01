/**
 * 汎用テーブル閲覧 API (`GET /api/db/tables/{table}`) の読み口。
 *
 * この API は 1 リクエストにつき既定 100 行しか返さない。引数なしで叩くと
 * 101 行目以降が黙って落ちるため、呼び出し側は必ずここを通す:
 *
 * - 画面が一覧をページ送りで見せるなら `fetchTablePage`
 *   (総件数は応答ヘッダ `X-Total-Count` から取る)
 * - 選択肢や照合など「全件そろっていること」が前提のところは
 *   `fetchAllTableRows` (上限いっぱいずつ offset をずらして読み切る)
 *
 * **総件数ヘッダは「取れたら使う」もので、continuation の根拠にはしない。**
 * 新しいフロントを古いバックエンド (ヘッダを付けない世代) に当てた起動や、
 * ブラウザにキャッシュされた古いフロントでは `X-Total-Count` が読めない。
 * そこで「見えている件数」を総件数と推定すると、続きがあるのに「これで
 * 全部」と読み違えて静かに切り捨てる — この一連の修正が退治している欠陥
 * そのものの再生産になる。なので継続判定は常に「返ってきたページが要求
 * した上限より短いか」で行い、総件数は表示にだけ使う (取れなければ件数の
 * 表示を落とす)。
 */

/** 1 ページの行数。WorldEditor の一覧はこの単位で送る */
export const DB_TABLE_PAGE_SIZE = 100;

/** サーバ側が 1 リクエストで許す最大行数 (api/routes/db_manager.py と対応) */
export const DB_TABLE_MAX_ROWS_PER_REQUEST = 1000;

/**
 * `fetchAllTableRows` が 1 回の呼び出しで読むページ数の上限。
 * 10 万行 (= 100 ページ × 1000 行) を超えるテーブルは管理 UI の想定外。
 */
const MAX_PAGES_PER_FETCH_ALL = 100;

export interface DbTablePage<T> {
    /** 要求したページに実際に入っていた行 */
    rows: T[];
    /**
     * テーブル全体の行数。応答ヘッダ `X-Total-Count` が読めなければ `null`
     * (「続きがあるか」は rows の長さで判定すること — 上のコメント参照)。
     */
    total: number | null;
}

function parseTotal(res: Response): number | null {
    const raw = res.headers.get('X-Total-Count');
    const parsed = raw === null ? NaN : Number(raw);
    if (Number.isFinite(parsed) && parsed >= 0) return parsed;
    return null;
}

/** テーブルの 1 ページ分を取る。失敗したら例外を投げる */
export async function fetchTablePage<T>(
    table: string,
    offset: number = 0,
    limit: number = DB_TABLE_PAGE_SIZE,
): Promise<DbTablePage<T>> {
    const res = await fetch(`/api/db/tables/${table}?limit=${limit}&offset=${offset}`);
    if (!res.ok) {
        throw new Error(`GET /api/db/tables/${table} failed: ${res.status}`);
    }
    const rows = (await res.json()) as T[];
    return { rows, total: parseTotal(res) };
}

/**
 * テーブルの全行を取る。上限いっぱいずつ offset をずらし、**要求した上限より
 * 短いページが返るまで**読み続けるので、100 行を超えるテーブルでも、ちょうど
 * 上限と同じ行数のテーブルでも欠けない。失敗したら例外を投げる。
 *
 * 並行して行が増減すると欠落・重複しうる (offset ページングの性質) が、この口
 * は管理 UI の人間操作用で、取得中の並行大量変更は運用上想定しない。完全性が
 * 要る用途にはサーバー側での一括取得を設けること。1000 行ちょうどで早期終了
 * する穴 (総件数ヘッダの推定値に頼っていた頃のもの) は、短いページまで読み
 * 続ける形にして塞いだ。
 */
export async function fetchAllTableRows<T>(table: string): Promise<T[]> {
    const collected: T[] = [];
    let offset = 0;
    for (let page = 0; page < MAX_PAGES_PER_FETCH_ALL; page++) {
        const result = await fetchTablePage<T>(table, offset, DB_TABLE_MAX_ROWS_PER_REQUEST);
        collected.push(...result.rows);
        offset += result.rows.length;
        if (result.rows.length < DB_TABLE_MAX_ROWS_PER_REQUEST) {
            return collected;
        }
    }
    // 打ち切りを黙って成功にしない — 呼び出し側は「全件」を前提にしている
    console.error(
        `fetchAllTableRows(${table}): ${MAX_PAGES_PER_FETCH_ALL} ページ ` +
        `(${collected.length} 行) で全件取得を打ち切った。以降の行は欠けている`,
    );
    return collected;
}
