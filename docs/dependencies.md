# Dependencies

Written 2026-11-14 for v2.1 Part 4 #18: "Pin every package version, keep a list of what
each one is for, and tell me before adding any new one."

Every version is pinned exactly, sub-dependencies included, in `requirements.txt`
(the service) and `requirements-dev.txt` (tests only, never in the image). The pins
were installed into an empty environment from these two files and all 435 tests
passed, on Python 3.12.3 and on Python 3.13.18. No package was added for v2 or v2.1.
`python-dotenv` was removed with Discord on 2026-11-13.

## The service (`requirements.txt`)

| Package | Version | What it is for |
| --- | --- | --- |
| aiohttp | 3.14.5 | The HTTP client for every GET to the public market-data APIs (`http.py`), and the dashboard's small web server (`dashboard.py`) |
| aiohappyeyeballs | 2.7.1 | aiohttp: connecting over IPv4 and IPv6 at once |
| aiosignal | 1.4.0 | aiohttp: internal callbacks |
| attrs | 26.1.0 | aiohttp: internal data classes |
| frozenlist | 1.8.0 | aiohttp: internal lists |
| idna | 3.20 | yarl: host names in URLs |
| multidict | 7.2.1 | aiohttp, yarl: HTTP headers and query strings |
| propcache | 0.5.4 | aiohttp, yarl: cached properties |
| typing_extensions | 4.16.0 | aiohttp, aiosignal: type hints |
| yarl | 1.25.1 | aiohttp: URLs |
| PyYAML | 6.0.3 | Reading `config.yaml` (`config.py`, `version.py`) |

Everything else the code uses (SQLite, asyncio, the statistics, the dashboard's HTML
and charts) is the Python standard library or the project's own code.

## Tests only (`requirements-dev.txt`)

| Package | Version | What it is for |
| --- | --- | --- |
| pytest | 9.2.0 | Running the tests |
| pytest-asyncio | 1.4.1.post1 | Async tests (the service is asyncio) |
| iniconfig | 2.3.1 | pytest: reading its settings |
| packaging | 26.3 | pytest: version checks |
| pluggy | 1.7.0 | pytest: plugins |
| Pygments | 2.21.0 | pytest: coloured failure output |

## The base image

`python:3.12-slim`, pinned by digest in the `Dockerfile`:
`sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`. That is the
multi-platform index the Docker registry served for the tag on 2026-11-14 (last
updated 2026-11-08), so Docker picks the right image for the server's processor. A
rebuild uses exactly this base until the digest is changed on purpose.

## Rules

* A version changes only on purpose, with the tests run on the new pins.
* A new package is proposed to the owner first, with what it is for, its license and
  why the standard library will not do.
* Code from other repositories is read and reviewed before any of it is used, and none
  of it runs on the server.

## In this sandbox, not in the project

Two packages are installed in the sandbox's own environment and are not project
dependencies. Neither is in the image:

* `ruff` 0.17.4: a code checker I run before each commit (`ruff check --select F,E9`).
  Adding it to `requirements-dev.txt` is the owner's call.
* `python-dotenv` 1.2.4: left over from before Discord was removed. Nothing imports it.
