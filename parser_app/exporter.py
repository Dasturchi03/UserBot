from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from parser_app.db import Database


HEADERS = ["user_id", "username", "first_name", "last_activity_date", "status"]


async def export_users(db: Database, export_dir: Path, only_new: bool, file_format: str = "xlsx") -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    rows = await db.export_rows(only_new)
    suffix = "new" if only_new else "all"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = export_dir / f"users_{suffix}_{stamp}.{file_format}"

    data = [[row[h] for h in HEADERS] for row in rows]
    if file_format == "csv":
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(HEADERS)
            writer.writerows(data)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Users"
        ws.append(HEADERS)
        for item in data:
            ws.append(item)
        wb.save(path)

    if only_new:
        await db.mark_exported([int(row["user_id"]) for row in rows])
    return path
