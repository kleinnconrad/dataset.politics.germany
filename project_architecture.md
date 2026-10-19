# German Politics Dataset: Architecture

This document describes the structure of the pipeline: scope and decisions, datasets and variables, sources, storage, the run, and the implementation status. The pipeline adopts the structure of [style_dataset](https://github.com/kleinnconrad/style_dataset). Usage and configuration are described in the [README](README.md).

## Table of Contents
- [1. Scope and Decisions](#1-scope-and-decisions)
- [2. Design Rules](#2-design-rules)
- [3. Datasets and Variables](#3-datasets-and-variables)
  - [3.1 Common Fields](#31-common-fields)
  - [3.2 Entities](#32-entities)
  - [3.3 Structured Datasets](#33-structured-datasets)
  - [3.4 News and Press Releases](#34-news-and-press-releases)
  - [3.5 Text Annotations](#35-text-annotations)
- [4. Prediction Targets](#4-prediction-targets)
- [5. Sources and Fetch Rules](#5-sources-and-fetch-rules)
- [6. Storage and Partitioning](#6-storage-and-partitioning)
- [7. Repository Structure](#7-repository-structure)
- [8. Pipeline Run](#8-pipeline-run)
- [9. Failure Handling](#9-failure-handling)
- [10. Implementation Status](#10-implementation-status)

---

## 1. Scope and Decisions

- **Scope:** Germany only: federal level, the 16 states, and the elections to the European Parliament held in Germany. `jurisdiction` is `DE` or an ISO 3166-2 state code (`DE-BY`, `DE-SN`, ...).
- **Repository:** public. Every record names the license of its source; the data is published under the ODbL 1.0 (see [data/LICENSE.md](data/LICENSE.md)); no full texts of articles or press releases are stored.
- **Article pages:** fetched for metadata and lead text where `robots.txt` and the text-and-data-mining reservations of the host permit it. The lead text is used for the annotation and then discarded.
- **Schedule:** one workflow runs the due collectors on Tuesdays and Fridays at 02:07 Europe/Berlin, the schedule of style_dataset. A run takes at most 30 minutes; the history is imported once by a separate `backfill` run. The politician registry and the pageviews are updated once a week.
- **Model:** Google Gemini (`gemini-2.5-flash` by default, thinking off) labels up to 400 news articles and press releases per run, with the request handling of style_dataset. At the measured token use of about 2,500 input and 2,300 output tokens per request of ten items, this costs about $0.26 per run.

**Consequence of the schedule for news.** RSS feeds and news sitemaps list only recent items; a news sitemap covers 48 hours, many feeds less. With runs every three or four days and a lookback of 96 hours, the pipeline collects what the feeds and sitemaps still list at run time. Articles that have left a feed before the next run are missing, so news coverage is a sample, not a complete record. Changing the cron line of `pipeline.yml` to a daily schedule closes most of this gap, at a Gemini cost of about $0.26 per day instead of per half week; the other collectors only fetch changes and are not affected by the frequency.

---

## 2. Design Rules

1. **Point-in-time correctness.** Every record has `observed_at`, the UTC time at which the pipeline observed it, in addition to its own event date. Filtering `observed_at <= t` reproduces what was known at time t. Items older than the first run of their collector (more than 7 days) have `backfilled: true`.
2. **Events and changes.** Event datasets store each item once; a changed item gets a new record with the next `revision`. Change datasets store a record whenever a tracked value changes.
3. **Raw data and annotations are separated.** Gemini labels are stored in `text_annotations`, keyed by item and `extraction_hash`, so that they can be regenerated with another model or prompt without changing the raw data.
4. **No full texts.** Only titles, URLs, metadata and derived variables are stored.
5. **Public actors only.** Office holders, candidates, parties and organizations. Names of other persons that appear in texts are counted, not stored; author names are not stored.
6. **Deterministic first.** Polls, votes, procedures, pageviews and article metadata are parsed without a model. Gemini is used only for text.

---

## 3. Datasets and Variables

All fields are defined with descriptions in [src/schema.py](src/schema.py).

### 3.1 Common Fields

Every record has: `record_id`, `revision`, `dataset`, `schema_version`, `observed_at`, `source_id`, `source_url`, `license`, `jurisdiction`, `backfilled`.

### 3.2 Entities

- **Parties** ([src/entities.py](src/entities.py)): fixed codes such as `CDU_CSU`, `CDU`, `CSU`, `SPD`, `GRUENE`, `FDP`, `AFD`, `LINKE`, `BSW`, `FW`, `SSW`, `VOLT` and `OTHER`, with aliases, DAWUM ids and candidate Wikipedia titles.
- **Politicians** (`data/state/politicians.json`): the current members of the Bundestag and the 16 state parliaments from abgeordnetenwatch, keyed by the abgeordnetenwatch id, with party, mandates and Wikidata id. The annotation resolves the names returned by Gemini against this registry: a full-name match, or a last-name match that is unique within the party named by the model. Federal ministers and other office holders without a mandate are not in the registry yet.

### 3.3 Structured Datasets

| Dataset | Kind | Variables |
|---|---|---|
| `polls` | event | poll_id, parliament, institute, tasker, method, fieldwork_start, fieldwork_end, published_date, sample_size, results {party: share}, others_share |
| `wikipedia_pageviews` | event | entity_id, entity_type, wikipedia_title, wikidata_qid, daily_views {date: views} for the days since the previous run |
| `roll_call_votes` | event | vote_id, parliament_period, vote_date, title, topics, committees, printed_papers (Drucksache numbers, the join key to the DIP), accepted, yes, no, abstain, no_show, by_faction, cohesion (Rice index), defectors |
| `mp_votes` | event | vote_id, vote_date, politician_id, faction, vote, voted_against_faction |
| `parliamentary_procedures` | change | procedure_id, change (new, status_changed, updated), procedure_type, electoral_period, title, initiators, subject_areas, descriptors, consent_required, status, previous_status, gesta_id, dip_updated_at, latest_document_date |
| `politicians` | change | politician_id, name, party (or NONE), sex, year_of_birth, wikidata_qid, mandates (parliament, period, faction, constituency, electoral list, mandate_won, start and end date) |

Counting rules of the votes: the majority of a group compares its yes and no votes; on a tie nobody counts as a defector. Members without a group (`NONE`) appear in `by_faction` but not in `cohesion` or `defectors`.

### 3.4 News and Press Releases

**Outlet attributes** (in `config/sources.toml`, copied into each record): `outlet_type`, `owner_group`, `home_state`, and the paywall model. Outlets have no political-leaning label; models can learn outlet effects from `source_id`.

**`news_items`:** outlet_id, outlet_type, owner_group, home_state, channel (rss or news_sitemap), url, canonical_url, title, published_at, updated_at, section, feed_categories, keywords, jsonld_type, is_agency_copy, author_count, word_count, is_paywalled, has_video, content_basis (title_only, title_teaser, lead_text).

**`press_releases`:** press_source_id, actor_type (federal government, federal ministry, Bundestag, Bundesrat, parliamentary group, party, state government, court), party, url, canonical_url, title, published_at, feed_categories, content_basis.

Variables that follow from these fields, such as the weekday, the hour or the delay between publication and observation, are computed in the analysis, not stored.

### 3.5 Text Annotations

`text_annotations` holds the Gemini labels of news articles and press releases.

| Group | Variables |
|---|---|
| Relevance | is_political, political_level (municipal, state, federal, EU, international), states_concerned |
| Topic | cap_topic, cap_topics_secondary, issue_detail |
| Form | article_type (news, opinion, interview, analysis, live blog, explainer, press release) |
| Event | event_types, event_status (announced or planned, ongoing, concluded), government_involved |
| Actors | mentioned_parties, party_roles {party: initiator, target, commentator}, quoted_parties, mentioned_politicians, unresolved_person_count |
| Tone | overall_tone, party_tone, politician_tone, government_tone |
| Framing | frame, conflict_pairs |
| Polls | reports_poll, poll_institute |
| Upcoming events | upcoming_events [{event_type, date}] |
| Press releases only | communication_type, populism_markers (anti-elite, people-centric, exclusionary) |

- `cap_topic` uses the major topics of the Comparative Agendas Project.
- `frame` uses the five generic news frames of Semetko and Valkenburg (2000): conflict, economic consequences, responsibility, human interest, morality.
- `event_types` is a fixed list: election campaign, election result, poll report, coalition conflict, coalition formation, government decision, legislation, parliamentary debate or vote, resignation or dismissal, appointment, scandal or investigation, court ruling, protest or strike, party leadership change, intra-party conflict, party convention, foreign or security crisis, economic development, other.
- Input per item: title, teaser and up to 1,500 characters of lead text where permitted. Ten items per request; up to four requests at the same time (`GEMINI_CONCURRENCY`).
- Up to `MAX_ANNOTATIONS_PER_RUN` (400) items are annotated per run: all press releases first, then news items in turns across the outlets, newest first. The other new political items are stored in `news_items` and `press_releases` from the feed data, without a page request and without an annotation, so that the item counts per source stay complete and the annotated sample can be weighted.
- Politicians are stored only if their name resolves to the registry; other names are counted in `unresolved_person_count`.

---

## 4. Prediction Targets

| Target | Labels | Main features |
|---|---|---|
| Vote shares in federal and state elections | election results (planned dataset) | poll averages and house effects, pageviews, media share of voice and tone, incumbency |
| Poll movement over the next 2 to 4 weeks | `polls` | event counts and tone per party, pageview changes, press release volume |
| Passage of bills and time to passage | `parliamentary_procedures` | initiator, consent requirement, government majority, cohesion of the coalition groups |
| Coalition break, early election, resignation of ministers | news event types; governments (planned dataset) | coalition conflict and scandal counts, poll gap between coalition partners, cohesion trend |
| Votes of members against their parliamentary group | `mp_votes` | topic, government or opposition role, list or constituency mandate |

---

## 5. Sources and Fetch Rules

| Source | Access | License or terms | Datasets |
|---|---|---|---|
| DAWUM | `api.dawum.de`, whole database as one JSON file; `last_update.txt` for change checks | ODbL | `polls` |
| abgeordnetenwatch API v2 | JSON, at most 30 requests per minute | CC0 | `politicians`, `roll_call_votes`, `mp_votes` |
| Bundestag DIP API | JSON, API key, filter by update time | DIP terms of use | `parliamentary_procedures` |
| Wikimedia Pageviews API | JSON, descriptive User-Agent | CC0 | `wikipedia_pageviews` |
| MediaWiki API (de.wikipedia), Wikidata API | JSON | CC0 | article titles for `wikipedia_pageviews` |
| News outlets | RSS, news sitemaps, article pages | publisher rights, metadata only | `news_items` |
| Federal and state governments, ministries, parliaments, parliamentary groups, parties, courts | RSS and Atom | publisher rights, metadata only | `press_releases` |

The news outlets and press-release sources are listed in [config/sources.toml](config/sources.toml): 40 outlets (national and regional public broadcasters, national dailies and magazines, a tabloid, online outlets, and regional dailies for all 16 states) and 26 press-release sources (federal government, Bundestag, Bundesrat, federal ministries, parliamentary groups, parties, three state governments). Each entry was checked for robots.txt, TDM reservation, bot protection and feed content on 2026-10-19; the comment line above each entry records the result and the time span of its feeds.

**Reservations in plain text.** No host publishes a machine-readable TDMRep file, but 26 of the 40 outlets (including all ARD broadcasters) state a reservation under § 44b UrhG in plain-text comments of their robots.txt. For these outlets, `fetch_pages` is false: their article pages are not fetched, and their items are annotated from title and teaser only. Outlets that declare a machine-readable reservation (FAZ, B.Z.) or block automated requests (Die Zeit, Stern, the RND titles) are not included.

**Known data issues.** Some feeds state wrong time zones ("CEST" read as UTC, local time marked as "Z", winter offsets in summer); the collector parses German zone abbreviations and treats publication times more than 15 minutes in the future as unknown. Titles of the Funke group share central articles, so the same article can appear under several outlets. abgeordnetenwatch keeps the 2021-2026 periods of Berlin and Mecklenburg-Vorpommern as current until it switches to the new periods. The DIP electoral period is the constant `ELECTORAL_PERIOD = 21` in `collectors/procedures.py` and must be raised when the 22nd period starts.

**Fetch rules** ([src/web.py](src/web.py)):
- **robots.txt** is checked for every website request (feeds, sitemaps, pages) with the user agent `GermanPoliticsDataset`. The parser follows RFC 9309: the longest matching rule decides, so that `Allow: /SiteGlobals/Functions/RSSFeed` overrides `Disallow: /SiteGlobals/` on the government sites (Python's `urllib.robotparser` applies the first matching rule and would block these feeds). The documented APIs above are offered for programmatic use; their robots.txt files exclude crawlers from the API paths, so they are called under their API terms and rate limits instead (`respect_robots=False`).
- **Text-and-data-mining reservations** (TDMRep header, `/.well-known/tdmrep.json`, `tdm-reservation` meta tag) exclude a source (`tdm_reserved`).
- **Bot protection:** HTTP 401 or 403, a challenge page or a CAPTCHA sets the source to `blocked`; it is re-enabled only by hand in `data/state/sources.json`. Article pages that are blocked while the feed works are skipped for the rest of the run. No headless browser is used.
- **Pacing:** one request per second per host, longer if robots.txt sets a crawl delay (several government sites ask for 30 or 60 seconds); 2.1 seconds for abgeordnetenwatch. A host that requires a crawl delay of more than 120 seconds is not fetched. Server errors, rate limits and network errors are retried twice. The feeds collector loads the robots.txt of all feed hosts before it reads the feeds, so that a crawl delay usually passes while other hosts are read; the two sources with a crawl delay of 30 or 60 seconds (Bundesrat, BMUKN) are read from one feed each, without article pages. A host whose robots.txt cannot be fetched within 10 seconds is not requested again in the same run.
- **Broken sources:** after 7 consecutive failed runs a source is `broken` and is retried once a week.

---

## 6. Storage and Partitioning

```text
data/
├── LICENSE.md
├── news_items/year=2026/month=10/news_items_2026-10-20.json
├── polls/year=2026/month=10/polls_2026-10-20.json
├── ...
└── state/
    ├── seen/<dataset>.json   # written items with content hash and revision
    ├── collectors.json       # first run, last run, failures, cursor per collector
    ├── sources.json          # health status per news and press source
    ├── politicians.json      # politician registry
    └── run_log.jsonl         # one line per run
```

- **Partition key:** the UTC day of the run that observed the record, for all datasets. A run writes at most one file per dataset; records are merged by `record_id` and `revision`, so a repeated run on the same day replaces instead of duplicating.
- **Folder names:** `year=` and `month=` let DuckDB, Polars and pyarrow read a dataset as one table with partition pruning.
- **Format:** one JSON list per file with one record per line (compact and readable in git diffs).
- **Seen stores:** the keys of news and press items are kept for 30 days; the keys of polls, votes and procedures are kept permanently.
- **Size:** the backfill run writes the history (about 2 MB of polls, 0.6 MB of party pageviews, the procedures of the current electoral period). News is expected to dominate later growth. If the repository exceeds about 1 GB, the news datasets move to a separate data repository with the same layout.

---

## 7. Repository Structure

```text
german_politics_dataset/
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── ci.yml              # lint, tests, commit messages
│       ├── notify.yml          # run summary as a commit comment that mentions the owner
│       └── pipeline.yml        # due collectors, Tuesdays and Fridays 02:07 Europe/Berlin; backfill by hand
├── config/
│   └── sources.toml            # curated news outlets and press-release sources
├── data/                       # datasets, state and data license, committed by the workflow
├── src/
│   ├── main.py                 # entry point
│   ├── pipeline.py             # collector order, isolation, run log
│   ├── settings.py             # environment variables
│   ├── web.py                  # HTTP client with the fetch rules
│   ├── schema.py               # record models and Gemini response models
│   ├── entities.py             # parties and politician registry
│   ├── sources.py              # source configuration and health rules
│   ├── content.py              # metadata and lead text of article pages
│   ├── annotation.py           # Gemini labels
│   ├── storage.py, state.py, urls.py
│   ├── generate_dataset_overview.py
│   └── collectors/
│       ├── base.py             # collector interface and run context
│       ├── parliament.py       # politicians, roll-call votes (abgeordnetenwatch)
│       ├── procedures.py       # parliamentary procedures (DIP)
│       ├── polls.py            # polls (DAWUM)
│       ├── pageviews.py        # Wikipedia pageviews
│       └── feeds.py            # news, press releases, annotations
├── scripts/
│   └── run_summary.py          # run summary for notify.yml
├── tests/                      # offline tests with a fake web and a fake Gemini client
├── pyproject.toml
├── uv.lock
├── LICENSE                     # MIT, for the code
└── README.md
```

---

## 8. Pipeline Run

`src/main.py` starts the run; `src/pipeline.py` runs the collectors in this order. The feeds come right after the politician registry, because news items leave the feeds after a few days; the data of the later collectors can also be fetched by a later run.

1. **politicians** (once a week): reads the current members of the Bundestag and the 16 state parliaments from abgeordnetenwatch (about 66 requests, about 2.5 minutes), rebuilds the registry and writes new or changed entries.
2. **feeds:**
   1. Load the robots.txt of all feed hosts, then read the RSS/Atom feeds and news sitemaps of all due sources; set blocked and failing sources according to the rules. The log lists the items and new political items per source.
   2. Keep new items published within 96 hours; drop outlet items that are neither from a politics section nor contain a political term.
   3. Select up to MAX_ANNOTATIONS_PER_RUN items: press releases first, then news items in turns across the outlets.
   4. Process the selected items in turns across the sources: fetch the page where permitted (JSON-LD metadata, canonical URL, lead text), annotate ten items per Gemini request with up to GEMINI_CONCURRENCY requests at the same time, resolve the politicians, and write items and annotations every 100 items, in the order of the batches.
   5. Write the other new items from the feed data without labels.
3. **roll_call_votes:** reads the roll-call votes of the current and the previous electoral period that are not yet stored, with the individual votes; at most 120 new votes per run (no limit in a backfill run).
4. **parliamentary_procedures:** reads the DIP procedures of the current electoral period that were updated since the previous run and writes new procedures and changes.
5. **polls:** downloads the DAWUM database if `last_update.txt` changed and writes new and corrected polls.
6. **wikipedia_pageviews** (once a week): resolves the article titles and fetches the days since the last stored day per article (about 1,000 requests, about 5 minutes).
7. The workflow updates the README overview and commits `data/` and `README.md`. `notify.yml` then posts a summary of the run, with the Gemini tokens and their estimated cost, as a commit comment that mentions the repository owner.

**Run modes.** A regular run (`normal`) has a deadline of 25 minutes (`RUN_DEADLINE_MINUTES`) and a job timeout of 30 minutes. A `backfill` run imports the history of collectors 1 and 3 to 6 without the per-run caps and without the weekly interval (deadline 85 minutes, timeout 90 minutes); it is started once by hand. `shadow` and `dry-run` work on a copy of the state and commit nothing.

**Weekly collectors.** A collector with `min_days_between_runs` (6 for politicians and pageviews) runs only if its last complete run (`last_complete` in `collectors.json`) is at least that many days old, so that it runs in every second scheduled run. Collectors selected with `COLLECTORS` or `--collectors` run in any case.

**Expected duration of a regular run** (estimates from the measured first run):

| Step | Tuesday run | Friday run |
|---|---|---|
| Setup and commit | 1 min | 1 min |
| politicians | 2.5 min | not due |
| feeds: reading | 5 min | 5 min |
| feeds: pages and Gemini (400 items) | 5 to 6 min | 5 to 6 min |
| roll_call_votes, parliamentary_procedures, polls | 2 min | 2 min |
| wikipedia_pageviews | 5 min | not due |
| Total | about 21 min | about 14 min |

---

## 9. Failure Handling

- **A collector fails:** the error is logged, recorded in `collectors.json` and the run log, and the next collector starts. Data already written stays. The run exits with code 1.
- **A source fails:** the other sources continue; the source health rules apply (`broken`, `blocked`, `tdm_reserved`).
- **Gemini returns an unusable response:** the batch is split in halves down to single items; items without usable labels are written without annotation.
- **Gemini quota, call budget or API error:** the annotation stops; the remaining items are written without annotation. A quota or API error marks the collector as failed; reaching the call budget does not.
- **Run deadline:** after `RUN_DEADLINE_MINUTES` the collectors start no new work. A collector that stops early has the status `partial` and continues in the next run (roll-call votes: the remaining votes; procedures: the previous cursor is kept; pageviews: the remaining articles; feeds: the remaining items are written without annotation). A collector that has not started by the deadline is skipped, which marks the run as failed. Requests in progress at the deadline finish within their timeouts (Gemini: at most three attempts of 60 seconds; other requests: 20 seconds per attempt), so that the workflow commits before its timeout.
- **Run interrupted:** items and annotations are written every 100 items together with the seen store; a later run skips the written items. A cancelled or timed-out workflow commits nothing; the run deadline prevents the timeout.
- **Concurrent pushes:** the workflow pulls with rebase and retries the push up to three times.

---

## 10. Implementation Status

| Part | Status |
|---|---|
| Polls, Wikipedia pageviews, news, press releases, annotations, politicians, roll-call votes, procedures | implemented |
| Election results and calendar (`elections`) | planned |
| Government composition (`governments`, from Wikidata) | planned |
| Party donations above 35,000 EUR (bundestag.de) | planned |
| Prediction markets on German politics | planned |
| Story clusters across outlets (`story_id`, `story_size`) | planned |
| Feature tables for model training (`scripts/build_features.py`) | planned |
| Review sheet for the annotation and a hand-labelled sample to measure agreement | planned |
| Office holders without a mandate (federal ministers, state premiers) in the politician registry | planned |
| Roll-call votes of the state parliaments | planned |
