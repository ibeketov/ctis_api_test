#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["httpx", "rich"]
# ///
"""
Find clinical trials that are actively recruiting patients in a given city,
and save the first N matches to separate files.

CTIS has no city/postcode filter in /search, so this works in two steps:

  1) /search   -- narrow down server-side by free-text condition (optional)
  2) /retrieve -- for each candidate, check which countries have started
                  recruitment (mscInfo.hasRecruitmentStarted) and keep only
                  trial sites located in the target city

Recruitment status is exposed per country, not per individual site: a trial
site counts as actively recruiting when its country's mscInfo has BOTH
trialStatus == "Authorised" (not yet ended/withdrawn) AND
hasRecruitmentStarted == true (not "authorised, recruitment pending").
Neither flag alone is reliable: hasRecruitmentStarted stays true even after
a trial has ended, and trialStatus can be "Authorised" while recruitment
hasn't started yet.

The trial title isn't a top-level field in /retrieve -- it lives at
authorizedApplication.authorizedPartI.trialDetails.clinicalTrialIdentifiers.publicTitle.

The portal throttles requests to under 1/second per client IP and may block
IPs that generate heavy traffic, so every call here is paced accordingly --
expect this script to take noticeably longer than the API alone would allow.
All calls are made one at a time through this throttle (the rate limit is
per client IP, so concurrency wouldn't let us go any faster against this
API), which is why this script is plain synchronous code rather than async.

A user's postcode (e.g. 10178) isn't directly comparable to a trial site's
postcode (e.g. 10117 for a specific clinic) -- they're both in Berlin but
numerically unrelated, and CTIS has no geocoding of its own. So --postcode is
resolved to a city name via the free Zippopotam.us lookup first, then the
existing city-matching logic is reused unchanged.

/search itself has no city/postcode filter, but it does support other
criteria that narrow the candidate list server-side before any /retrieve
calls are made -- which matters a lot given the throttle above. The trickiest
one is recruitment status: /search has both a top-level "status" field (the
trial's OVERALL public status) and a sibling "mscStatus" field that filters
by the status SPECIFICALLY WITHIN the country given in "msc". Verified live
against the real API (condition=prostate): no status filter at all gives 229
results; msc=Germany + the old "status":[1,2,3,4] gives 43; msc=Germany +
"mscStatus":[3,4] (codes 3/4 = "Authorised, recruiting" / "Ongoing,
recruiting" -- the only two that mean recruitment is happening *now*) gives
32, with every result's trialCountries field confirmed to show "Germany:3" or
"Germany:4". Adding the old "status" filter on top changes nothing once
mscStatus is set, so this script sends mscStatus only (even with no country
selected, mscStatus=[3,4] alone still narrows 229->103). This is still not
sufficient on its own (mscStatus is trial+country-wide, not per-site, and its
semantics aren't proven 1:1 with /retrieve's mscInfo fields), so
recruiting_sites_in_city() re-checks each candidate individually -- it stays
the authority on what counts as "recruiting"; mscStatus is purely a
volume-reducing pre-filter.

--age-group and --gender are optional extra /search criteria (ageGroupCode,
gender), confirmed present in the live web search form, useful for narrowing
to trials a specific patient could actually be eligible for.

See USAGE.md for a quick-start guide and option reference.
"""

import argparse
import json
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Final

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

console: Final = Console()

BASE_URL: Final = "https://euclinicaltrials.eu/ctis-public-api"
SEARCH_URL: Final = f"{BASE_URL}/search"
RETRIEVE_URL_TEMPLATE: Final = f"{BASE_URL}/retrieve/{{ct_number}}"
SEARCH_PAGE_SIZE: Final = 50
OUTPUT_DIR: Final = Path(__file__).parent / "matched_trials"

# Free postcode -> place lookup, used only to turn --postcode into a city
# name. Unrelated to the CTIS portal, so it isn't subject to its throttling.
POSTCODE_LOOKUP_URL_TEMPLATE: Final = "https://api.zippopotam.us/{country}/{postcode}"

# ISO 3166-1 alpha-2 -> the numeric "msc" (member state concerned) codes
# accepted by /search's searchCriteria.msc. These are ISO 3166-1 numeric
# codes for the EEA countries CTIS covers (see search-api.yaml).
COUNTRY_MSC_CODES: Final[dict[str, int]] = {
    "at": 40,
    "be": 56,
    "bg": 100,
    "hr": 191,
    "cy": 196,
    "cz": 203,
    "dk": 208,
    "ee": 233,
    "fi": 246,
    "fr": 250,
    "de": 276,
    "gr": 300,
    "hu": 348,
    "is": 352,
    "ie": 372,
    "it": 380,
    "lv": 428,
    "li": 438,
    "lt": 440,
    "lu": 442,
    "mt": 470,
    "nl": 528,
    "no": 578,
    "pl": 616,
    "pt": 620,
    "ro": 642,
    "sk": 703,
    "si": 705,
    "es": 724,
    "se": 752,
}


class MscStatus(IntEnum):
    """search-api.yaml's mscStatus enum (per-country trial status)."""

    UNDER_EVALUATION = 1
    AUTHORISED_RECRUITMENT_PENDING = 2
    AUTHORISED_RECRUITING = 3
    ONGOING_RECRUITING = 4


# The only two statuses that mean recruitment is happening RIGHT NOW in that
# country -- see module docstring for the verified counts that justify using
# mscStatus instead of the older, trial-wide "status" field.
ACTIVELY_RECRUITING_MSC_STATUS_CODES: Final[list[int]] = [
    int(MscStatus.AUTHORISED_RECRUITING),
    int(MscStatus.ONGOING_RECRUITING),
]

# search-api.yaml's ageGroupCode and gender enums, exposed as --age-group/
# --gender so users can narrow to trials they'd actually be eligible for.
AGE_GROUP_CODES: Final[dict[str, int]] = {
    "in-utero": 1,
    "0-17": 2,
    "18-64": 3,
    "65+": 4,
}
GENDER_CODES: Final[dict[str, int]] = {"male": 1, "female": 2}

# The portal enforces a throttling rule of under 1 request/second per client
# IP and reserves the right to block IPs causing heavy traffic. MIN_REQUEST_INTERVAL
# keeps every call (search and retrieve) at least this far apart, regardless
# of how many trials need to be checked.
MIN_REQUEST_INTERVAL: Final = 1.1
_last_request_at: float = 0.0


@dataclass(slots=True, frozen=True)
class SavedTrial:
    ct_number: str
    title: str | None
    status: str | None
    path: Path


def throttled_request(
    client: httpx.Client, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    response = client.request(method, url, timeout=30, **kwargs)
    _last_request_at = time.monotonic()
    response.raise_for_status()
    return response


def search_page(
    client: httpx.Client,
    condition: str,
    msc_code: int | None,
    page: int,
    age_group_codes: list[int] | None = None,
    gender_codes: list[int] | None = None,
) -> dict[str, Any]:
    search_criteria: dict[str, Any] = {
        "containAll": "",
        "containAny": "",
        "containNot": "",
        "medicalCondition": condition,
        "mscStatus": ACTIVELY_RECRUITING_MSC_STATUS_CODES,
    }
    if msc_code is not None:
        search_criteria["msc"] = [msc_code]
    if age_group_codes:
        search_criteria["ageGroupCode"] = age_group_codes
    if gender_codes:
        search_criteria["gender"] = gender_codes

    payload = {
        "searchCriteria": search_criteria,
        "pagination": {"page": page, "size": SEARCH_PAGE_SIZE},
    }
    response = throttled_request(client, "POST", SEARCH_URL, json=payload)
    return response.json()


def retrieve_trial(client: httpx.Client, ct_number: str) -> dict[str, Any]:
    response = throttled_request(
        client, "GET", RETRIEVE_URL_TEMPLATE.format(ct_number=ct_number)
    )
    return response.json()


def resolve_city_from_postcode(
    client: httpx.Client, postcode: str, country: str
) -> str:
    url = POSTCODE_LOOKUP_URL_TEMPLATE.format(country=country, postcode=postcode)
    response = client.get(url, timeout=15)
    if response.status_code == 404:
        raise ValueError(f"Unknown postcode {postcode!r} for country {country!r}")
    response.raise_for_status()
    place = response.json()["places"][0]["place name"]
    console.print(
        f"[cyan]Resolved postcode {postcode} ({country}) -> [bold]{place}[/bold][/cyan]"
    )
    return place


def trial_title(trial: dict[str, Any]) -> str | None:
    identifiers = (
        trial.get("authorizedApplication", {})
        .get("authorizedPartI", {})
        .get("trialDetails", {})
        .get("clinicalTrialIdentifiers", {})
    )
    return identifiers.get("publicTitle") or identifiers.get("fullTitle")


def recruiting_sites_in_city(trial: dict[str, Any], city: str) -> list[dict[str, Any]]:
    """Return trial site addresses in `city` whose country is actively recruiting.

    This stays the authority on "is this trial actually recruiting here",
    even though /search's mscStatus filter already narrowed the candidates:
    mscStatus is trial+country-wide, not per-site, and its semantics aren't
    proven 1:1 with these /retrieve fields -- mscStatus is purely a
    volume-reducing pre-filter, not a replacement for this check.
    """
    matches = []
    for part in trial.get("authorizedApplication", {}).get("authorizedPartsII", []):
        msc = part.get("mscInfo", {})
        if msc.get("trialStatus") != "Authorised" or not msc.get(
            "hasRecruitmentStarted"
        ):
            continue
        for site in part.get("trialSites", []):
            address = site.get("organisationAddressInfo", {}).get("address")
            if (
                address
                and (address.get("city") or "").strip().lower() == city.strip().lower()
            ):
                matches.append(address)
    return matches


def save_trial(trial: dict[str, Any], ct_number: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{ct_number}.json"
    path.write_text(json.dumps(trial, indent=2, ensure_ascii=False))
    return path


def find_recruiting_trials_in_city(
    client: httpx.Client,
    city: str,
    count: int,
    condition: str,
    msc_code: int | None,
    age_group_codes: list[int] | None = None,
    gender_codes: list[int] | None = None,
) -> list[SavedTrial]:
    page_number = 1
    saved: list[SavedTrial] = []
    throttled = False

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} saved"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Starting search...", total=count)

        while len(saved) < count and not throttled:
            page = search_page(
                client, condition, msc_code, page_number, age_group_codes, gender_codes
            )
            total_records = page["pagination"]["totalRecords"]

            if page_number == 1:
                console.print(
                    f"[blue]Search API:[/blue] [bold]{total_records}[/bold] trial(s) found for "
                    f"condition={condition!r}, msc={msc_code!r}, "
                    f"mscStatus={ACTIVELY_RECRUITING_MSC_STATUS_CODES}, "
                    f"ageGroupCode={age_group_codes!r}, gender={gender_codes!r}"
                )

            candidates = page["data"]
            if not candidates:
                break

            for item in candidates:
                if len(saved) >= count:
                    break

                ct_number = item["ctNumber"]
                progress.update(task_id, description=f"Checking {ct_number}...")
                try:
                    trial = retrieve_trial(client, ct_number)
                except httpx.HTTPStatusError as error:
                    match error.response.status_code:
                        case 403 | 429:
                            console.print(
                                f"[red bold]Throttled[/red bold] by the portal while fetching "
                                f"{ct_number} ({error}). Stopping early to respect the rate limit."
                            )
                            throttled = True
                        case _:
                            console.print(
                                f"[yellow]Skipping {ct_number}: {error}[/yellow]"
                            )
                    if throttled:
                        break
                    continue

                sites = recruiting_sites_in_city(trial, city)
                if not sites:
                    continue

                title = trial_title(trial)
                status = trial.get("ctStatus")
                path = save_trial(trial, ct_number)
                saved.append(
                    SavedTrial(
                        ct_number=ct_number, title=title, status=status, path=path
                    )
                )
                progress.advance(task_id)
                console.print(f"[green bold]Saved[/green bold] {ct_number} -> {path}")
                console.print(f"  [dim]Title:[/dim]  {title}")
                console.print(f"  [dim]Status:[/dim] {status}")

            if throttled or not page["pagination"]["nextPage"]:
                break
            page_number += 1

        progress.update(task_id, description="Done.")

    return saved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument(
        "--city",
        help="City where recruitment must be ongoing, e.g. Berlin",
    )
    location.add_argument(
        "--postcode",
        help="Postcode to resolve to a city, e.g. 10178 (resolves to Berlin)",
    )
    parser.add_argument(
        "--country",
        default=None,
        help=(
            "ISO alpha-2 country code (e.g. de, fr, es). Required to resolve "
            "--postcode (defaults to 'de' in that case); also narrows /search "
            "to that country when given alongside --city. Omitted with --city "
            "means the search isn't narrowed by country."
        ),
    )
    parser.add_argument(
        "--count", type=int, default=2, help="Number of matching trials to save"
    )
    parser.add_argument(
        "--condition",
        required=True,
        help="Medical condition to search for, e.g. menopause, prostate, diabetes.",
    )
    parser.add_argument(
        "--age-group",
        choices=sorted(AGE_GROUP_CODES),
        action="append",
        default=None,
        help="Restrict to trials enrolling this age group (repeatable), e.g. --age-group 65+",
    )
    parser.add_argument(
        "--gender",
        choices=sorted(GENDER_CODES),
        action="append",
        default=None,
        help="Restrict to trials enrolling this gender (repeatable), e.g. --gender female",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    with httpx.Client() as client:
        if args.postcode:
            country_code = (args.country or "de").strip().lower()
            try:
                city = resolve_city_from_postcode(client, args.postcode, country_code)
            except ValueError as error:
                console.print(f"[red bold]Error:[/red bold] {error}")
                raise SystemExit(1)
        else:
            city = args.city
            country_code = args.country.strip().lower() if args.country else None

        if country_code is None:
            msc_code = None
            console.print(
                "[yellow]No --country given with --city: searching all countries.[/yellow]"
            )
        else:
            msc_code = COUNTRY_MSC_CODES.get(country_code)
            if msc_code is None:
                console.print(
                    f"[yellow]Warning: unknown country {country_code!r}, "
                    f"searching without a country filter[/yellow]"
                )

        age_group_codes = (
            [AGE_GROUP_CODES[g] for g in args.age_group] if args.age_group else None
        )
        gender_codes = [GENDER_CODES[g] for g in args.gender] if args.gender else None

        saved = find_recruiting_trials_in_city(
            client,
            city,
            args.count,
            args.condition,
            msc_code,
            age_group_codes,
            gender_codes,
        )

    console.print(
        f"\n[green bold]Saved {len(saved)}/{args.count}[/green bold] trial(s) "
        f"recruiting in [bold]{city}[/bold] to {OUTPUT_DIR}/"
    )


if __name__ == "__main__":
    main()
