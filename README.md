# search_recruiting_trials.py

Finds clinical trials (CTIS) actively recruiting patients in a given city or
postcode, for a given medical condition, and saves the matching trial
records as JSON files.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages the Python 3.14 interpreter and
  the `httpx`/`rich` dependencies automatically — nothing to install manually).

## Quick start

```bash
uv run search_recruiting_trials.py --postcode 10178 --count 5 --condition prostate
```

This resolves postcode `10178` to `Berlin`, finds trials matching "prostate"
that are actively recruiting in Germany, and saves the first 5 matches with
a site in Berlin to `matched_trials/<ctNumber>.json`.

## Options

| Option | Required | Description |
|---|---|---|
| `--city CITY` | one of `--city`/`--postcode` | City where recruitment must be ongoing, e.g. `Berlin` |
| `--postcode CODE` | one of `--city`/`--postcode` | Postcode resolved to a city via Zippopotam.us, e.g. `10178` |
| `--country CODE` | no | ISO alpha-2 country code (e.g. `de`, `es`, `fr`). Required to resolve `--postcode` (defaults to `de`); also narrows the search to that country when combined with `--city` |
| `--condition TEXT` | **yes** | Free-text medical condition, e.g. `prostate`, `diabetes`, `menopause` |
| `--count N` | no | Number of matching trials to save (default: `2`) |
| `--age-group GROUP` | no | Restrict to an age group: `in-utero`, `0-17`, `18-64`, `65+`. Repeatable. |
| `--gender GENDER` | no | Restrict to a gender: `male`, `female`. Repeatable. |

## More examples

```bash
# By city name, narrowed to Spain, only male patients
uv run search_recruiting_trials.py --city Madrid --country es --count 3 --condition prostate --gender male

# By postcode, two age groups at once
uv run search_recruiting_trials.py --postcode 10178 --count 3 --condition prostate --age-group 65+ --age-group 18-64

# City without a country: searches across all countries (slower, broader)
uv run search_recruiting_trials.py --city Berlin --count 3 --condition prostate
```

## Output

A live progress bar (via `rich`) tracks how many of the requested trials
have been found so far, with the current trial number being checked shown
next to it. For each match, the script prints the trial number, title and
status in colour, and writes the full `/retrieve` response to
`matched_trials/<ctNumber>.json`.

## Notes

- The portal throttles requests to under 1/second per client IP, so the
  script paces every call accordingly — expect it to take noticeably longer
  than a single API call would.
- There is no city/postcode filter in the underlying API; `--postcode` is
  resolved to a city name first, and the actual trial-site addresses are
  checked individually after fetching each candidate.
