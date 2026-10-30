
import os
import requests

from dotenv import load_dotenv
from storage_manager import process_domain_updates


def collect_domains(session=None):

    load_dotenv()

    public_key = os.getenv("DATTO_PUBLIC_KEY")
    secret_key = os.getenv("DATTO_SECRET_KEY")
    base_url = os.getenv("DATTO_BASE_URL")

    if not all([public_key, secret_key, base_url]):
        raise ValueError("Missing Datto API configuration")

    url = f"{base_url.rstrip('/')}/v1/saas/domains"

    client = session if session is not None else requests

    try:
        response = client.get(
            url,
            auth=(public_key, secret_key),
            headers={"Accept": "application/json"},
            timeout=90
        )

        response.raise_for_status()

        domains = response.json()

        if not isinstance(domains, list):
            raise ValueError("Unexpected Domains API response format")

        result = process_domain_updates(domains)

        print("DOMAINS COLLECTION SUMMARY")
        print("Status:", result["status"])

        return result

    except requests.RequestException as error:

        print(
            f"ERROR: Domains API collection failed: "
            f"{type(error).__name__}"
        )

        return {
            "status": "failed",
            "error": type(error).__name__
        }


if __name__ == "__main__":

    print(
        "Domains collector initialised. "
        "Live API collection is not enabled yet."
    )
