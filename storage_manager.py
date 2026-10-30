import json
from pathlib import Path

from datetime import datetime, timezone
from uuid import uuid4

# Application project directory
BASE_DIR = Path(__file__).resolve().parent

# JSON storage directory
DATA_DIR = BASE_DIR / "data"

# Domain data file
DOMAINS_FILE = DATA_DIR / "domains.json"

HISTORY_FILE = DATA_DIR / "history.jsonl"

TRANSACTION_FILE = DATA_DIR / "pending_transaction.json"

SEATS_FILE = DATA_DIR / "seats.json"

APPLICATIONS_FILE = DATA_DIR / "applications.json"


def initialize_storage():
    # Automatically create data directory
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create domains.json only if it doesn't exist
    if not DOMAINS_FILE.exists():

        initial_data = {
            "last_updated": None,
            "total_domains": 0,
            "domains": []
        }

        with open(DOMAINS_FILE, "x", encoding="utf-8") as file:
            json.dump(initial_data, file, indent=4)

        print("SUCCESS: domains.json created")

    else:
        print("INFO: Existing domains.json preserved")

    # Initialize seats storage only on first execution
    if not SEATS_FILE.exists():

            initial_seats = {
                "last_updated": None,
                "total_customers": 0,
                "total_seats": 0,
                "customers": {}
            }

            with open(SEATS_FILE, "x", encoding="utf-8") as file:
                json.dump(initial_seats, file, indent=4)

            print("SUCCESS: seats.json created")

    else:
            print("INFO: Existing seats.json preserved")

    # Initialise Applications storage on first execution
    if not APPLICATIONS_FILE.exists():

        initial_applications = {
            "last_updated": None,
            "total_customers": 0,
            "customers": {}
        }

        with open(APPLICATIONS_FILE, "x", encoding="utf-8") as file:
            json.dump(initial_applications, file, indent=4)

        print("SUCCESS: applications.json created")

    else:
        print("INFO: Existing applications.json preserved")

    print("Storage initialization completed successfully")


def save_domains(domains):
    # Only accept a valid API response containing a list
    if not isinstance(domains, list):
        raise ValueError("Invalid API response: Expected a list")

    # Validate the response before saving
    for domain in domains:
        if not isinstance(domain, dict) or "saasCustomerId" not in domain:
            raise ValueError("Invalid domain record received")

    from datetime import datetime, timezone
    import os

    # Ensure storage exists
    initialize_storage()

    # Prepare the JSON data
    domain_data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_domains": len(domains),
        "domains": domains
    }

    # Write safely to a temporary file first
    temporary_file = DATA_DIR / "domains.json.tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(domain_data, file, indent=4)
            file.flush()
            os.fsync(file.fileno())

        # Replace the existing JSON only after successful writing
        os.replace(temporary_file, DOMAINS_FILE)

    finally:
        temporary_file.unlink(missing_ok=True)

    print(f"SUCCESS: Saved {len(domains)} SaaS Protection domains")

    return domain_data


def detect_domain_changes(new_domains):
    # Read the existing JSON data
    with open(DOMAINS_FILE, "r", encoding="utf-8") as file:
        stored_data = json.load(file)

    old_domains = stored_data.get("domains", [])

    # Identify each domain using its customer ID and domain name
    def domain_key(record):
        return (
            record["saasCustomerId"],
            record["domain"]
        )

    old_records = {
        domain_key(record): record
        for record in old_domains
    }

    new_records = {
        domain_key(record): record
        for record in new_domains
    }

    # Detect new domains
    added = [
        record for key, record in new_records.items()
        if key not in old_records
    ]

    # Detect modified domains
    modified = [
        record for key, record in new_records.items()
        if key in old_records
           and record != old_records[key]
    ]

    # Detect removed domains
    removed = [
        record for key, record in old_records.items()
        if key not in new_records
    ]

    print("CHANGE DETECTION RESULTS")
    print("Added:", len(added))
    print("Modified:", len(modified))
    print("Removed:", len(removed))

    return {
        "added": added,
        "modified": modified,
        "removed": removed
    }


def append_domain_history(
    changes,
    previous_domains=None,
    transaction_id=None
):
    from datetime import datetime, timezone

    # Validate input before writing
    required_keys = ("added", "modified", "removed")

    if not isinstance(changes, dict) or not all(
            key in changes and isinstance(changes[key], list)
            for key in required_keys
    ):
        raise ValueError("Invalid change detection results")

    # Load previous domain values
    if previous_domains is None:
        with open(DOMAINS_FILE, "r", encoding="utf-8") as file:
            previous_domains = json.load(file)["domains"]

    previous_records = {
        (record["saasCustomerId"], record["domain"]): record
        for record in previous_domains
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).isoformat()
    events = []

    for change_type in required_keys:
        for record in changes[change_type]:
            key = (record["saasCustomerId"], record["domain"])

            event = {
                "timestamp": timestamp,
                "transaction_id": transaction_id,
                "event_type": change_type.upper(),
                "saasCustomerId": record["saasCustomerId"],
                "domain": record["domain"],
                "previous": previous_records.get(key),
                "current": (
                    None if change_type == "removed" else record
                )
            }

            events.append(event)

    # Do not append anything when data is unchanged
    if not events:
        print("INFO: No domain changes to record")
        return 0

    # Append historical events without overwriting prior history
    with open(HISTORY_FILE, "a", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event) + "\n")

        file.flush()

        import os
        os.fsync(file.fileno())

    print(f"SUCCESS: Appended {len(events)} domain history events")

    return len(events)


def validate_domain_records(domains):
    if not isinstance(domains, list):
        raise ValueError("Invalid response: Expected a list")

    if not domains:
        raise ValueError(
            "Empty domain response requires manual verification"
        )

    identities = set()

    for index, domain in enumerate(domains):

        if not isinstance(domain, dict):
            raise ValueError(
                f"Invalid domain record at index {index}"
            )

        customer_id = domain.get("saasCustomerId")
        domain_name = domain.get("domain")

        if (
                isinstance(customer_id, bool)
                or not isinstance(customer_id, int)
                or (
                domain_name is not None
                and (
                        not isinstance(domain_name, str)
                        or not domain_name.strip()
                )
        )
        ):
            raise ValueError(
                f"Missing or invalid identity at index {index}"
            )

        identity = (customer_id, domain_name)

        if identity in identities:
            raise ValueError(
                "Duplicate domain identity detected"
            )

        identities.add(identity)

    print(
        f"VALIDATION PASSED: {len(domains)} unique domain records"
    )

    return True


def write_transaction_journal(transaction):

    import os

    # Validate transaction structure
    required_fields = (
        "transaction_id",
        "timestamp",
        "previous_domains",
        "new_domains"
    )

    if not isinstance(transaction, dict):
        raise ValueError("Invalid transaction data")

    for field in required_fields:
        if field not in transaction:
            raise ValueError(
                f"Missing transaction field: {field}"
            )

    if not isinstance(transaction["previous_domains"], list):
        raise ValueError("Invalid previous domain records")

    if not isinstance(transaction["new_domains"], list):
        raise ValueError("Invalid new domain records")

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Never overwrite an unresolved transaction
    if TRANSACTION_FILE.exists():
        raise RuntimeError(
            "Pending transaction exists. Recovery required."
        )

    temporary_file = DATA_DIR / "pending_transaction.json.tmp"

    try:
        # Write transaction safely to temporary storage
        with open(temporary_file, "x", encoding="utf-8") as file:
            json.dump(transaction, file, indent=4)
            file.flush()
            os.fsync(file.fileno())

        # Publish transaction journal
        os.replace(temporary_file, TRANSACTION_FILE)

    finally:
        temporary_file.unlink(missing_ok=True)

    print("SUCCESS: Transaction journal created")

    return TRANSACTION_FILE


def read_transaction_journal():

    # No pending transaction
    if not TRANSACTION_FILE.exists():
        return None

    # Read the existing transaction journal
    with open(TRANSACTION_FILE, "r", encoding="utf-8") as file:
        transaction = json.load(file)

    # Validate journal structure
    required_fields = (
        "transaction_id",
        "timestamp",
        "previous_domains",
        "new_domains"
    )

    if not isinstance(transaction, dict):
        raise ValueError("Invalid transaction journal format")

    for field in required_fields:
        if field not in transaction:
            raise ValueError(
                f"Corrupted transaction journal: Missing {field}"
            )

    if not isinstance(transaction["transaction_id"], str):
        raise ValueError("Invalid transaction ID")

    if not isinstance(transaction["timestamp"], str):
        raise ValueError("Invalid transaction timestamp")

    if not isinstance(transaction["previous_domains"], list):
        raise ValueError("Invalid previous domain snapshot")

    if not isinstance(transaction["new_domains"], list):
        raise ValueError("Invalid new domain snapshot")

    print("INFO: Pending transaction journal detected")

    return transaction


def transaction_exists_in_history(transaction_id):

    if not isinstance(transaction_id, str) or not transaction_id:
        raise ValueError("Invalid transaction ID")

    # No history file means no recorded transactions
    if not HISTORY_FILE.exists():
        return False

    with open(HISTORY_FILE, "r", encoding="utf-8") as file:

        for line_number, line in enumerate(file, start=1):

            if not line.strip():
                raise ValueError(
                    f"Empty history entry at line {line_number}"
                )

            try:
                event = json.loads(line)

            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Corrupted history at line {line_number}"
                ) from error

            if not isinstance(event, dict):
                raise ValueError(
                    f"Invalid history event at line {line_number}"
                )

            if event.get("transaction_id") == transaction_id:
                return True

    return False


def count_transaction_history_events(transaction_id):

    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise ValueError("Invalid transaction ID")

    if not HISTORY_FILE.exists():
        return 0

    event_count = 0

    with open(HISTORY_FILE, "r", encoding="utf-8") as file:

        for line_number, line in enumerate(file, start=1):

            if not line.strip():
                raise ValueError(
                    f"Empty history entry at line {line_number}"
                )

            try:
                event = json.loads(line)

            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Corrupted history at line {line_number}"
                ) from error

            if not isinstance(event, dict):
                raise ValueError(
                    f"Invalid history event at line {line_number}"
                )

            if event.get("transaction_id") == transaction_id:
                event_count += 1

    return event_count


def complete_domain_transaction(transaction):

    transaction_id = transaction["transaction_id"]
    expected_events = transaction["expected_events"]

    # Confirm the journal belongs to this transaction
    pending = read_transaction_journal()

    if pending is None:
        raise RuntimeError("Transaction journal is missing")

    if pending != transaction:
        raise RuntimeError("Transaction journal mismatch")

    # Verify historical event count
    recorded_events = count_transaction_history_events(
        transaction_id
    )

    if recorded_events != expected_events:
        raise RuntimeError(
            f"History incomplete: expected {expected_events}, "
            f"found {recorded_events}"
        )

    # Verify the current snapshot matches the intended update
    with open(DOMAINS_FILE, "r", encoding="utf-8") as file:
        current_snapshot = json.load(file)

    if current_snapshot["domains"] != transaction["new_domains"]:
        raise RuntimeError(
            "Current snapshot does not match transaction"
        )

    # Clear journal only after successful verification
    TRANSACTION_FILE.unlink()

    print("SUCCESS: Transaction committed and journal cleared")

    return True


def process_domain_updates(new_domains):
    # Validate incoming domain records before processing
    validate_domain_records(new_domains)

    initialize_storage()

    # Prevent updates while an unresolved transaction exists
    pending_transaction = read_transaction_journal()

    if pending_transaction is not None:
        raise RuntimeError(
            "Pending transaction detected. "
            "Recovery must complete before new updates."
        )

    # Detect changes against existing records
    changes = detect_domain_changes(new_domains)


    # Safeguard against incomplete API responses
    if changes["removed"]:
        print(
            "WARNING: API response contains missing domain records."
        )
        print(
            "WARNING: Existing snapshot preserved pending verification."
        )

        return {
            "status": "verification_required",
            "changes": 0,
            "suspected_removals": len(changes["removed"])
        }


    total_changes = sum(
        len(records) for records in changes.values()
    )

    # Preserve existing snapshot when unchanged
    if total_changes == 0:
        print("INFO: No changes detected. Existing JSON preserved.")
        return {
            "status": "unchanged",
            "changes": 0
        }


    # Prepare a unique transaction for this update
    with open(DOMAINS_FILE, "r", encoding="utf-8") as file:
        previous_snapshot = json.load(file)


    transaction = {
        "transaction_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "previous_domains": previous_snapshot["domains"],
        "new_domains": new_domains,
        "expected_events": total_changes
    }



    print("INFO: Transaction prepared successfully")
    print(f"INFO: Expected history events: {transaction['expected_events']}")

    # Persist recovery information before modifying data
    write_transaction_journal(transaction)

    # Record changes with the transaction ID
    events_written = append_domain_history(
        changes,
        previous_domains=transaction["previous_domains"],
        transaction_id=transaction["transaction_id"]
    )

    # Update current snapshot
    saved_snapshot = save_domains(new_domains)

    # Verify successful completion and clear journal
    complete_domain_transaction(transaction)

    print("SUCCESS: Domain update completed")

    return {
        "status": "updated",
        "changes": events_written,
        "total_domains": saved_snapshot["total_domains"]
    }


def save_customer_seats(customer_id, seats):

    import os
    from datetime import datetime, timezone

    if isinstance(customer_id, bool) or not isinstance(customer_id, int):
        raise ValueError("Invalid SaaS customer ID")

    if not isinstance(seats, list):
        raise ValueError("Seats response must be a list")

    if not all(isinstance(seat, dict) for seat in seats):
        raise ValueError("Invalid seat record detected")

    initialize_storage()

    # Load existing customer data
    with open(SEATS_FILE, "r", encoding="utf-8") as file:
        stored_data = json.load(file)

    customers = stored_data.get("customers", {})
    customer_key = str(customer_id)

    # Ignore differences caused only by seat ordering
    def normalize(records):
        return sorted(
            records,
            key=lambda record: json.dumps(record, sort_keys=True)
        )

    existing = customers.get(customer_key, {}).get("seats")

    if existing is not None and normalize(existing) == normalize(seats):
        print(f"INFO: No seat changes for customer {customer_id}")
        return "unchanged"

    # Update only the selected customer
    customers[customer_key] = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "seat_count": len(seats),
        "seats": seats
    }

    stored_data["customers"] = customers
    stored_data["total_customers"] = len(customers)
    stored_data["total_seats"] = sum(
        len(customer.get("seats", []))
        for customer in customers.values()
    )
    stored_data["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Write to a temporary file before replacing current storage
    temporary_file = DATA_DIR / "seats.json.tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(stored_data, file, indent=4)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_file, SEATS_FILE)

    finally:
        temporary_file.unlink(missing_ok=True)

    print(f"SUCCESS: Saved {len(seats)} seats for customer {customer_id}")

    return "updated"


def save_customer_applications(customer_id, applications):

    import os
    from datetime import datetime, timezone

    if isinstance(customer_id, bool) or not isinstance(customer_id, int):
        raise ValueError("Invalid SaaS customer ID")

    if not isinstance(applications, list):
        raise ValueError("Applications response must be a list")

    # Validate the documented response structure
    for index, page in enumerate(applications):

        if not isinstance(page, dict):
            raise ValueError(
                f"Invalid Applications response at index {index}"
            )

        if not isinstance(page.get("items"), list):
            raise ValueError(
                f"Missing or invalid items at index {index}"
            )

        if not isinstance(page.get("pagination"), dict):
            raise ValueError(
                f"Missing pagination at index {index}"
            )

    # Avoid overwriting valid data with an empty response
    if not applications:
        raise ValueError("Empty Applications response requires verification")

    initialize_storage()

    with open(APPLICATIONS_FILE, "r", encoding="utf-8") as file:
        stored_data = json.load(file)

    customers = stored_data.get("customers", {})
    customer_key = str(customer_id)

    # Preserve unchanged application data
    existing = customers.get(customer_key, {}).get("applications")

    if existing == applications:
        print(f"INFO: No application changes for customer {customer_id}")
        return "unchanged"

    timestamp = datetime.now(timezone.utc).isoformat()

    customers[customer_key] = {
        "last_updated": timestamp,
        "applications": applications
    }

    stored_data["customers"] = customers
    stored_data["total_customers"] = len(customers)
    stored_data["last_updated"] = timestamp

    temporary_file = DATA_DIR / "applications.json.tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(stored_data, file, indent=4)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_file, APPLICATIONS_FILE)

    finally:
        temporary_file.unlink(missing_ok=True)

    print(
        f"SUCCESS: Applications data saved for customer {customer_id}"
    )

    return "updated"


if __name__ == "__main__":
    initialize_storage()
