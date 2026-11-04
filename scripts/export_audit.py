#!/usr/bin/env python3
"""审计导出（implement.md 阶段 7）：把 action_log 导出为 CSV/JSON。

用法：
    python scripts/export_audit.py --last 24h --format csv --out /tmp/audit.csv
    python scripts/export_audit.py --last all --format json
参数：
    --db     缺省 /var/lib/aiops/aiops.db（也可用 AIOPS_DB_PATH）
    --last   时间窗口：1h / 24h / 7d / all（缺省 24h）
    --format csv | json（缺省 csv）
    --out    输出文件（缺省 stdout）
说明：只读打开数据库（mode=ro），不影响 Router 写入。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


def parse_last(value: str) -> str | None:
    if value == "all":
        return None
    unit = value[-1]
    amount = int(value[:-1])
    delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount),
             "m": timedelta(minutes=amount)}.get(unit)
    if delta is None:
        raise SystemExit(f"非法 --last: {value}（支持 1h/24h/7d/all）")
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel 审计导出")
    parser.add_argument("--db", default=os.environ.get("AIOPS_DB_PATH",
                                                       "/var/lib/aiops/aiops.db"))
    parser.add_argument("--last", default="24h")
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--out")
    args = parser.parse_args()

    since = parse_last(args.last)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, incident_id, task_id, operator, action, detail_json, created_at FROM action_log"
    params: tuple = ()
    if since:
        sql += " WHERE created_at >= ?"
        params = (since,)
    sql += " ORDER BY id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()

    if args.format == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=1)
    else:
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["id", "incident_id", "task_id",
                                                 "operator", "action",
                                                 "detail_json", "created_at"])
        writer.writeheader()
        writer.writerows(rows)
        text = buf.getvalue()

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print(f"已导出 {len(rows)} 条审计 → {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
