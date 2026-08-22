"""Verify the whole Google Cloud chain works before anything depends on it.

Checks, in order:
  1. Application default credentials exist
  2. The project resolves and Vertex AI is reachable
  3. A Gemini model actually answers

Model availability is regional and does not match intuition: at the time of
writing, Gemini 3.5 is not served from us-central1 at all, but is served from
the `global` endpoint. So this sweeps model x location rather than assuming a
pair, and reports the first combination that answers.

Run this before writing agent code. A wrong region costs five minutes to fix
today and half a day to diagnose on day six.

    python scripts/smoke_test.py <PROJECT_ID> [LOCATION]

With no LOCATION, every candidate location is tried.
"""

from __future__ import annotations

import sys

# The hackathon requires Gemini 3.5 or newer, so those come first and the
# older model is only a fallback that proves the plumbing works.
CANDIDATE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-3-flash",
    "gemini-3-pro",
    "gemini-2.5-flash",
]

# `global` first: it covers all regions and is where the newest models land
# earliest. The rest are regions Gemini 3.5 is documented as serving from.
CANDIDATE_LOCATIONS = [
    "global",
    "northamerica-northeast1",
    "europe-west2",
    "asia-southeast1",
    "us-central1",
]

REQUIRED_PREFIXES = ("gemini-3",)


def _short(exc: Exception) -> str:
    return str(exc).split("\n")[0][:100]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    project = sys.argv[1]
    locations = [sys.argv[2]] if len(sys.argv) > 2 else CANDIDATE_LOCATIONS

    print(f"project   {project}")
    print(f"locations {', '.join(locations)}")
    print()

    try:
        from google import genai
    except ImportError:
        print("FAIL  google-genai is not installed  ->  pip install google-genai")
        return 1

    found: list[tuple[str, str]] = []

    for location in locations:
        try:
            client = genai.Client(vertexai=True, project=project, location=location)
        except Exception as exc:  # noqa: BLE001
            print(f"--    {location:<24} client error: {_short(exc)}")
            continue

        for model in CANDIDATE_MODELS:
            if any(m == model for m, _ in found):
                continue
            try:
                response = client.models.generate_content(
                    model=model,
                    contents="Reply with the single word: ready",
                )
                text = (response.text or "").strip()
                print(f"OK    {model:<20} @ {location:<24} -> {text[:40]}")
                found.append((model, location))
            except Exception as exc:  # noqa: BLE001
                print(f"--    {model:<20} @ {location:<24} {_short(exc)}")

    print()
    if not found:
        print("FAIL  nothing answered anywhere.")
        print("      Check the API is on and billing is linked:")
        print(f"        gcloud services list --enabled --project={project} | grep aiplatform")
        print(f"        gcloud billing projects describe {project}")
        return 1

    # The hackathon rules require Gemini 3.5 or newer, so prefer one of those
    # even if an older model also answered.
    compliant = [(m, l) for m, l in found if m.startswith(REQUIRED_PREFIXES)]
    chosen = compliant[0] if compliant else found[0]
    model, location = chosen

    if not compliant:
        print("WARN  only pre-3.x models answered.")
        print("      The hackathon requires Gemini 3.5 or newer, so this")
        print("      configuration would not satisfy the rules as they stand.")
        print()

    print(f"PASS  use {model} @ {location}")
    print()
    print("Set this in your .env:")
    print(f"  GOOGLE_GENAI_USE_VERTEXAI=true")
    print(f"  GOOGLE_CLOUD_PROJECT={project}")
    print(f"  GOOGLE_CLOUD_LOCATION={location}")
    print(f"  FIELDPILOT_MODEL={model}")
    return 0 if compliant else 1


if __name__ == "__main__":
    sys.exit(main())
