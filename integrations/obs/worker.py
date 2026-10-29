"""SDK worker entry point; provider imports happen after stdout is claimed."""
import sys

from midimap_integration_sdk import package_manifest, run_worker


def create_provider():
    from obs_provider import ObsProvider
    return ObsProvider()


if __name__ == "__main__":
    sys.exit(run_worker(create_provider, package_manifest(__file__)))
