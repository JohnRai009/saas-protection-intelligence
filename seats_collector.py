import json
import os
import requests

from dotenv import load_dotenv
from storage_manager import DOMAINS_FILE, save_customer_seats


def get_saas_customers():
    with open(DOMAINS_FILE, 'r', encoding='utf-8') as file:
        stored_data = json.load(file)
    domains = stored_data.get('domains', [])
    customer_ids = {
        record['saasCustomerId'] for record in domains
        if isinstance(record, dict)
        and isinstance(record.get('saasCustomerId'), int)
        and not isinstance(record['saasCustomerId'], bool)
    }
    return sorted(customer_ids)


def fetch_customer_seats(customer_id, session=None):
    if not isinstance(customer_id, int) or isinstance(customer_id, bool):
        raise ValueError('Invalid SaaS customer ID')
    load_dotenv()
    public_key = os.getenv('DATTO_PUBLIC_KEY')
    secret_key = os.getenv('DATTO_SECRET_KEY')
    base_url = os.getenv('DATTO_BASE_URL')
    if not all([public_key, secret_key, base_url]):
        raise ValueError('Missing Datto API configuration')
    url = f"{base_url.rstrip('/')}/v1/saas/{customer_id}/seats"
    client = session if session is not None else requests
    response = client.get(url, auth=(public_key, secret_key),
                          headers={'Accept': 'application/json'}, timeout=90)
    response.raise_for_status()
    seats = response.json()
    if not isinstance(seats, list):
        raise ValueError('Unexpected Seats API response format')
    for index, seat in enumerate(seats):
        if not isinstance(seat, dict):
            raise ValueError(f'Invalid seat record at index {index}')
    return seats


def collect_customer_seats(customer_id, session=None):
    try:
        seats = fetch_customer_seats(customer_id, session=session)
        storage_status = save_customer_seats(customer_id, seats)
        return {'customer_id': customer_id, 'status': 'success',
                'storage': storage_status, 'seat_count': len(seats)}
    except (requests.RequestException, ValueError) as error:
        print(f'ERROR: Seats collection failed for customer {customer_id}: {type(error).__name__}')
        return {'customer_id': customer_id, 'status': 'failed', 'error': type(error).__name__}


def collect_all_customer_seats(session=None, progress_callback=None):
    customer_ids = get_saas_customers()
    results = []
    print('Starting SaaS Protection Seats Collection')
    print(f'Total customers: {len(customer_ids)}')
    for index, customer_id in enumerate(customer_ids, start=1):
        if progress_callback is not None:
            progress_callback(index, len(customer_ids), customer_id, None, 'Seats')
        result = collect_customer_seats(customer_id, session=session)
        results.append(result)
        if progress_callback is not None:
            progress_callback(index, len(customer_ids), customer_id, result, 'Seats')
    successful = sum(result['status'] == 'success' for result in results)
    failed = len(results) - successful
    summary = {'total_customers': len(customer_ids), 'successful': successful,
               'failed': failed, 'results': results}
    print('\nSEATS COLLECTION SUMMARY')
    print('Customers processed:', len(customer_ids))
    print('Successful:', successful)
    print('Failed:', failed)
    return summary


if __name__ == '__main__':
    customers = get_saas_customers()
    print('DATTO SAAS PROTECTION - SEATS COLLECTOR')
    print('Unique SaaS Customers:', len(customers))
    print('Customer ID extraction: SUCCESS')
