"""SDK worker entry point; imports the provider after stdout is claimed."""
import sys

from midimap_integration_sdk import package_manifest, run_worker


def create_provider():
    from google_provider import GoogleHomeProvider
    return GoogleHomeProvider()


if __name__ == "__main__":
    sys.exit(run_worker(create_provider, package_manifest(__file__)))
