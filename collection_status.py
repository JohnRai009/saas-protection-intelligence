
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent / "data"
STATUS_FILE = DATA_DIR / "collection_status.json"


def save_collection_status(summary):
    """
    Atomically save the latest collection run summary.
    Does not change any customer data or existing storage files.
    """

    if not isinstance(summary, dict):
        raise ValueError("Collection summary must be a dictionary")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    status = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "started_at": summary.get("started_at"),
        "completed_at": summary.get("completed_at"),
        "customers": summary.get("customers", 0),
        "domains": summary.get("domains", {}),
        "seats": summary.get("seats", {}),
        "applications": summary.get("applications", {})
    }

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=DATA_DIR,
            prefix=".collection_status_",
            suffix=".tmp",
            delete=False
        ) as temporary_file:

            temporary_path = Path(temporary_file.name)

            json.dump(status, temporary_file, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, STATUS_FILE)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return status


def load_collection_status():

    if not STATUS_FILE.exists():
        return None

    with STATUS_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:
        return json.load(file)


if __name__ == "__main__":
    print(
        "Collection Status module initialised. "
        "No status file has been written."
    )
