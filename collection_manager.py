"""Coordinate Datto collection and publish atomic UI progress snapshots."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from domains_collector import collect_domains
from collection_status import save_collection_status
from seats_collector import get_saas_customers, collect_all_customer_seats
from applications_collector import collect_all_customer_applications
from storage_manager import DOMAINS_FILE

PROGRESS_FILE = Path(__file__).resolve().parent / 'data' / 'collection_progress.json'
RUN_HISTORY_FILE = PROGRESS_FILE.parent / 'collection_runs.jsonl'


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _write_progress(payload):
    """Atomic replacement: the dashboard never reads half-written JSON."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=PROGRESS_FILE.parent,
                                         prefix='.progress_', suffix='.tmp', delete=False) as handle:
            name = handle.name
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, PROGRESS_FILE)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _customer_names():
    try:
        with open(DOMAINS_FILE, encoding='utf-8') as handle:
            domains = json.load(handle).get('domains', [])
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    result = {}
    for item in domains:
        if isinstance(item, dict):
            cid = item.get('saasCustomerId')
            if isinstance(cid, int) and not isinstance(cid, bool):
                result[cid] = (item.get('saasCustomerName') or
                               item.get('organizationName') or f'Customer {cid}')
    return result


def run_collection(session=None):
    started_at = _timestamp()
    progress = {
        'state': 'running', 'stage': 'Domains', 'completed': 0, 'total': 0,
        'customer_id': None, 'customer_name': 'Discovering customers',
        'processing': False,
        'seats_completed': 0, 'applications_completed': 0,
        'seats_failed': 0, 'applications_failed': 0,
        'started_at': started_at, 'updated_at': started_at,
    }

    def update(**fields):
        progress.update(fields)
        progress['updated_at'] = _timestamp()
        _write_progress(progress)

    update()
    print('=' * 60)
    print('DATTO SAAS PROTECTION - COLLECTION MANAGER')
    print('=' * 60)

    try:
        print('\nStarting Domains collection')
        domains_result = collect_domains(session=session)
        print('Domains status:', domains_result['status'])
        customer_ids = get_saas_customers()
        names = _customer_names()
        total = len(customer_ids)
        print(f'Customers identified: {total}')
        update(stage='Seats', total=total, completed=0,
               customer_name='Preparing seat collection')

        def seats_progress(index, count, customer_id, result, phase):
            update(stage='Seats', completed=index if result is not None else index - 1,
                   processing=result is None,
                   seats_completed=index if result is not None else index - 1,
                   customer_id=customer_id,
                   customer_name=names.get(customer_id, f'Customer {customer_id}'),
                   seats_failed=progress['seats_failed'] + (result is not None and result.get('status') != 'success'))

        def apps_progress(index, count, customer_id, result, phase):
            update(stage='Applications', completed=index if result is not None else index - 1,
                   processing=result is None,
                   applications_completed=index if result is not None else index - 1,
                   customer_id=customer_id,
                   customer_name=names.get(customer_id, f'Customer {customer_id}'),
                   applications_failed=progress['applications_failed'] + (result is not None and result.get('status') != 'success'))

        print('\nStarting Seats collection')
        seats_result = collect_all_customer_seats(session=session, progress_callback=seats_progress)
        update(stage='Applications', completed=0, customer_id=None,
               customer_name='Preparing applications collection', processing=False)
        print('\nStarting Applications collection')
        applications_result = collect_all_customer_applications(session=session, progress_callback=apps_progress)

        summary = {
            'started_at': started_at, 'completed_at': _timestamp(),
            'customers': total, 'domains': domains_result,
            'seats': {'successful': seats_result['successful'], 'failed': seats_result['failed'],
                      'results': seats_result.get('results', [])},
            'applications': {'successful': applications_result['successful'], 'failed': applications_result['failed'],
                             'results': applications_result.get('results', [])},
        }
        print('\nCOLLECTION SUMMARY')
        print('Customers:', total)
        print('Seats successful:', summary['seats']['successful'])
        print('Seats failed:', summary['seats']['failed'])
        print('Applications successful:', summary['applications']['successful'])
        print('Applications failed:', summary['applications']['failed'])
        save_collection_status(summary)
        # Append one factual record per completed run; never overwrite earlier runs.
        RUN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RUN_HISTORY_FILE.open('a', encoding='utf-8') as run_log:
            run_log.write(json.dumps(summary, ensure_ascii=False) + '\n')
        print('\nCollection status saved successfully')
        domains_ok = str(domains_result.get('status', '')).lower() in (
            'updated', 'unchanged', 'success', 'no_change', 'no_changes')
        all_ok = (domains_ok and total > 0 and
                  seats_result['successful'] == total and seats_result['failed'] == 0 and
                  applications_result['successful'] == total and applications_result['failed'] == 0)
        update(state='success' if all_ok else 'partial', stage='Completed',
               completed=total, customer_id=None,
               customer_name='All customers processed', processing=False, completed_at=_timestamp())
        return summary
    except Exception as exc:
        update(state='failed', stage='Failed', customer_name=type(exc).__name__,
               error=type(exc).__name__, completed_at=_timestamp())
        raise


if __name__ == '__main__':
    print('Collection Manager initialised. Live collection is not enabled yet.')
