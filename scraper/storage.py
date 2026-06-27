"""
CSV writer with per-row flush and JSON checkpoint for resume capability.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = [
    "dealer_name",
    "guns_com_profile_url",
    "location_city",
    "location_state",
    "location_address",
    "phone",
    "email",
    "website",
    "total_listings",
    "new_listings",
    "used_listings",
    "ffl_verified",
    "dealer_rating",
    "review_count",
    "scraped_at",
]


def _checkpoint_path(csv_path: str) -> Path:
    return Path(csv_path).with_suffix(".checkpoint.json")


class CSVWriter:
    def __init__(self, csv_path: str, resume: bool = False):
        self.csv_path = Path(csv_path)
        self.checkpoint_path = _checkpoint_path(csv_path)
        self.resume = resume
        self._last_index = -1
        self._file = None
        self._writer = None
        self._open(resume)

    def _open(self, resume: bool) -> None:
        mode = "a" if resume and self.csv_path.exists() else "w"
        self._file = open(self.csv_path, mode, newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=COLUMNS, extrasaction="ignore")
        if mode == "w":
            self._writer.writeheader()
            self._file.flush()

    def load_checkpoint(self) -> int:
        """Return the last successfully written dealer index (0-based), or -1 if none."""
        if self.checkpoint_path.exists():
            try:
                data = json.loads(self.checkpoint_path.read_text())
                return int(data.get("last_index", -1))
            except Exception:
                pass
        return -1

    def write_row(self, dealer: dict, index: int) -> None:
        row = {col: dealer.get(col, "") for col in COLUMNS}
        row["scraped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._writer.writerow(row)
        self._file.flush()
        self._save_checkpoint(index)

    def _save_checkpoint(self, index: int) -> None:
        self.checkpoint_path.write_text(json.dumps({"last_index": index}))

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
