"""Datto SaaS Protection Applications collector; verified 30-day history."""
import os
import requests
from dotenv import load_dotenv
from seats_collector import get_saas_customers
from storage_manager import save_customer_applications

PAGE_SIZE = 25
HISTORY_DAYS = 30
MAX_PAGES = 1000
REQUEST_TIMEOUT = 90


def _validate_page(raw_data):
    if isinstance(raw_data, dict):
        pages = [raw_data]
    elif isinstance(raw_data, list):
        pages = raw_data
    else:
        raise ValueError('Unexpected Applications API response format')
    if not pages:
        raise ValueError('Applications API returned no page envelope')
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError('Applications API page must be an object')
        if not isinstance(page.get('items'), list):
            raise ValueError('Applications API page is missing an items list')
        if not isinstance(page.get('pagination'), dict):
            raise ValueError('Applications API page is missing pagination metadata')
    return pages


def _total_pages(pages):
    values = []
    for page in pages:
        value = page['pagination'].get('totalPages')
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError('Invalid or missing pagination.totalPages')
        values.append(value)
    if len(set(values)) != 1:
        raise ValueError('Conflicting Applications pagination totals')
    return values[0]


def fetch_customer_applications(customer_id, session=None):
    if isinstance(customer_id, bool) or not isinstance(customer_id, int):
        raise ValueError('Invalid SaaS customer ID')
    load_dotenv()
    public_key = os.getenv('DATTO_PUBLIC_KEY')
    secret_key = os.getenv('DATTO_SECRET_KEY')
    base_url = os.getenv('DATTO_BASE_URL')
    if not all([public_key, secret_key, base_url]):
        raise ValueError('Missing Datto API configuration')
    url = f"{base_url.rstrip('/')}/v1/saas/{customer_id}/applications"
    client = session if session is not None else requests
    pages = []
    expected_total = None
    page_number = 1
    while True:
        if page_number > MAX_PAGES:
            raise ValueError('Applications pagination exceeded safety limit')
        response = client.get(
            url, auth=(public_key, secret_key),
            headers={'Accept': 'application/json'},
            params={'_page': page_number, '_perPage': PAGE_SIZE, 'daysUntil': HISTORY_DAYS},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        batch = _validate_page(response.json())
        reported_total = _total_pages(batch)
        if expected_total is None:
            expected_total = reported_total
            if expected_total > MAX_PAGES:
                raise ValueError('Applications page count exceeds safety limit')
        elif reported_total != expected_total:
            raise ValueError('Applications page count changed during collection')
        if len(pages) + len(batch) > expected_total:
            raise ValueError('Applications API returned more pages than advertised')
        pages.extend(batch)
        if len(pages) == expected_total:
            return pages
        if len(batch) != 1:
            raise ValueError('Ambiguous Applications API paging response')
        if not batch[0]['items']:
            raise ValueError('Empty Applications page before final page')
        page_number += 1


def collect_customer_applications(customer_id, session=None):
    try:
        applications = fetch_customer_applications(customer_id, session=session)
        storage_status = save_customer_applications(customer_id, applications)
        print(f'SUCCESS: Applications collected for customer {customer_id}')
        print('Pages retrieved:', len(applications))
        print('Application response items:', sum(len(page['items']) for page in applications))
        print('History requested (days):', HISTORY_DAYS)
        return {'customer_id': customer_id, 'status': 'success', 'storage': storage_status}
    except (requests.RequestException, ValueError) as error:
        status_code = None
        if isinstance(error, requests.HTTPError) and error.response is not None:
            status_code = error.response.status_code
        print(f'ERROR: Applications collection failed for customer {customer_id}')
        print('HTTP Status:', status_code if status_code is not None else 'Not available')
        print('Error Type:', type(error).__name__)
        print('Error Details:', str(error))
        print('Existing application snapshot preserved.')
        return {'customer_id': customer_id, 'status': 'failed', 'error': type(error).__name__}


def collect_all_customer_applications(session=None, progress_callback=None):
    customer_ids = get_saas_customers()
    results = []
    print('Starting SaaS Protection Applications Collection')
    print(f'Total customers: {len(customer_ids)}')
    for index, customer_id in enumerate(customer_ids, start=1):
        if progress_callback is not None:
            progress_callback(index, len(customer_ids), customer_id, None, 'Applications')
        result = collect_customer_applications(customer_id, session=session)
        results.append(result)
        if progress_callback is not None:
            progress_callback(index, len(customer_ids), customer_id, result, 'Applications')
    successful = sum(result['status'] == 'success' for result in results)
    failed = len(results) - successful
    summary = {'total_customers': len(customer_ids), 'successful': successful,
               'failed': failed, 'results': results}
    print('\nAPPLICATIONS COLLECTION SUMMARY')
    print('Customers processed:', len(customer_ids))
    print('Successful:', successful)
    print('Failed:', failed)
    return summary


if __name__ == '__main__':
    customers = get_saas_customers()
    print('DATTO SAAS PROTECTION - APPLICATIONS COLLECTOR')
    print('Unique SaaS Customers:', len(customers))
    print('Applications API function initialised successfully')
    print('Live Applications collection is not enabled from this entry point')
