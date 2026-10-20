# German Politics Dataset
[![CI](https://github.com/kleinnconrad/german_politics_dataset/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kleinnconrad/german_politics_dataset/actions/workflows/ci.yml)

> [!IMPORTANT]
> **The DIP API key expires on 31 May 2027.** Before that date, take the current key from the [DIP help page](https://dip.bundestag.de/%C3%BCber-dip/hilfe/api), replace the repository secret `DIP_API_KEY` (**Settings > Secrets and variables > Actions**) and the value in your local `.env`, and update the expiry date in `.github/workflows/pipeline.yml` (`DIP_KEY_VALID_UNTIL`) and in this note. The pipeline workflow shows a warning from 45 days before the date and fails from 14 days before, so that GitHub sends a notification.

A dataset of German politics for political analysis and the prediction of elections and other political events. A pipeline in GitHub Actions runs twice a week (Tuesdays and Fridays) for at most 30 minutes. It collects opinion polls, roll-call votes, parliamentary procedures, Wikipedia pageviews, news articles and press releases from public APIs, feeds and websites without bot protection, labels the news and press releases with Google Gemini (`gemini-2.5-flash`), and stores everything as partitioned JSON files in this repository.

## Table of Contents
- [Dataset Overview](#dataset-overview)
- [Datasets](#datasets)
- [Data Model](#data-model)
- [Sources and Fetch Rules](#sources-and-fetch-rules)
- [Pipeline](#pipeline)
- [Data Files](#data-files)
- [Workflows](#workflows)
- [Configuration](#configuration)
- [Running Locally](#running-locally)
- [Continuous Integration](#continuous-integration)
- [Setup](#setup)
- [License](#license)

## Dataset Overview
<!-- DATASET_OVERVIEW_START -->
**Last updated:** 2026-10-20 04:51 UTC

| Dataset | Records | Items | Files | Observed | Content |
|---|---|---|---|---|---|
| `polls` | 3,980 | 3,980 | 1 | 2026-10-19 to 2026-10-19 | One published opinion poll (Sonntagsfrage). |
| `wikipedia_pageviews` | 993 | 993 | 1 | 2026-10-19 to 2026-10-19 | Daily views of the German Wikipedia article of a party or politician. |
| `news_items` | 2,572 | 2,572 | 1 | 2026-10-20 to 2026-10-20 | Metadata of one news article. The text is not stored. |
| `press_releases` | 128 | 128 | 1 | 2026-10-20 to 2026-10-20 | Metadata of one press release. The text is not stored. |
| `text_annotations` | 398 | 398 | 1 | 2026-10-20 to 2026-10-20 | Gemini labels of one news article or press release. |
| `roll_call_votes` | 247 | 247 | 1 | 2026-10-19 to 2026-10-19 | One roll-call vote (namentliche Abstimmung) in the Bundestag. |
| `mp_votes` | 172,677 | 172,677 | 1 | 2026-10-19 to 2026-10-19 | The vote of one member in one roll-call vote. |
| `parliamentary_procedures` | 19,265 | 19,265 | 1 | 2026-10-19 to 2026-10-19 | One parliamentary procedure (Vorgang) in the DIP, recorded when it appears or changes. |
| `politicians` | 2,513 | 2,513 | 1 | 2026-10-19 to 2026-10-19 | A politician with a current mandate, recorded when the entry appears or changes. |

**Runs**

- **Latest run:** 2026-10-20 04:38 UTC (normal)
  - `feeds`: ok (news_items 2,572, press_releases 128, text_annotations 398)
  - `parliamentary_procedures`: ok
  - `politicians`: not_due
  - `polls`: ok
  - `roll_call_votes`: ok
  - `wikipedia_pageviews`: not_due

**Sources**

- **News outlets:** 40 configured (40 active)
- **Press-release sources:** 26 configured (25 active, 1 blocked)

<details>
<summary>News outlets: items per source</summary>

| Source | Type | Status | Items | Last 30 days | Annotated | With lead text | Latest publication |
|---|---|---|---|---|---|---|---|
| Augsburger Allgemeine | regional daily | active | 96 | 96 | 7.3% (7) | 0.0% (0) | 2026-10-20 |
| Berliner Zeitung | regional daily | active | 33 | 33 | 21.2% (7) | 21.2% (7) | 2026-10-19 |
| Bild | tabloid | active | 161 | 161 | 4.3% (7) | 4.3% (7) | 2026-10-20 |
| BR24 | public broadcaster regional | active | 32 | 32 | 21.9% (7) | 0.0% (0) | 2026-10-20 |
| buten un binnen | public broadcaster regional | active | 3 | 3 | 100.0% (3) | 0.0% (0) | 2026-10-19 |
| Der Spiegel | weekly magazine | active | 85 | 85 | 8.2% (7) | 0.0% (0) | 2026-10-20 |
| Der Tagesspiegel | national daily | active | 133 | 133 | 5.3% (7) | 0.0% (0) | 2026-10-20 |
| Deutschlandfunk | public broadcaster national | active | 10 | 10 | 70.0% (7) | 70.0% (7) | 2026-10-19 |
| Die Welt | national daily | active | 222 | 222 | 3.2% (7) | 1.8% (4) | 2026-10-20 |
| Focus Online | weekly magazine | active | 120 | 120 | 5.8% (7) | 4.2% (5) | 2026-10-20 |
| Freie Presse | regional daily | active | 8 | 8 | 87.5% (7) | 0.0% (0) | 2026-10-20 |
| Hamburger Abendblatt | regional daily | active | 111 | 111 | 5.4% (6) | 2.7% (3) | 2026-10-20 |
| Handelsblatt | business | active | 69 | 69 | 10.1% (7) | 10.1% (7) | 2026-10-20 |
| hessenschau.de | public broadcaster regional | active | 4 | 4 | 100.0% (4) | 0.0% (0) | 2026-10-19 |
| Lausitzer Rundschau | regional daily | active | 44 | 44 | 15.9% (7) | 0.0% (0) | 2026-10-20 |
| MDR | public broadcaster regional | active | 34 | 34 | 20.6% (7) | 0.0% (0) | 2026-10-20 |
| n-tv.de | online only | active | 107 | 107 | 6.5% (7) | 6.5% (7) | 2026-10-20 |
| NDR | public broadcaster regional | active | 15 | 15 | 46.7% (7) | 0.0% (0) | 2026-10-20 |
| Neue Osnabrücker Zeitung | regional daily | active | 215 | 215 | 3.3% (7) | 0.0% (0) | 2026-10-20 |
| Nordkurier | regional daily | active | 82 | 82 | 8.5% (7) | 8.5% (7) | 2026-10-20 |
| Nürnberger Nachrichten | regional daily | active | 30 | 30 | 23.3% (7) | 0.0% (0) | 2026-10-20 |
| rbb24 | public broadcaster regional | active | 17 | 17 | 41.2% (7) | 0.0% (0) | 2026-10-20 |
| Rheinische Post | regional daily | active | 91 | 91 | 7.7% (7) | 0.0% (0) | 2026-10-20 |
| Saarbrücker Zeitung | regional daily | active | 13 | 13 | 53.8% (7) | 53.8% (7) | 2026-10-20 |
| shz.de | regional daily | active | 85 | 85 | 8.2% (7) | 0.0% (0) | 2026-10-20 |
| Stuttgarter Zeitung | regional daily | active | 59 | 59 | 11.9% (7) | 0.0% (0) | 2026-10-20 |
| SWR Aktuell | public broadcaster regional | active | 7 | 7 | 100.0% (7) | 0.0% (0) | 2026-10-20 |
| Süddeutsche Zeitung | national daily | active | 22 | 22 | 31.8% (7) | 0.0% (0) | 2026-10-19 |
| Südkurier | regional daily | active | 51 | 51 | 13.7% (7) | 0.0% (0) | 2026-10-20 |
| t-online | online only | active | 71 | 71 | 9.9% (7) | 0.0% (0) | 2026-10-20 |
| tagesschau.de | public broadcaster national | active | 70 | 70 | 10.0% (7) | 0.0% (0) | 2026-10-20 |
| taz | national daily | active | 56 | 56 | 12.5% (7) | 0.0% (0) | 2026-10-19 |
| Thüringer Allgemeine | regional daily | active | 92 | 92 | 5.4% (5) | 0.0% (0) | 2026-10-20 |
| Trierischer Volksfreund | regional daily | active | 22 | 22 | 31.8% (7) | 31.8% (7) | 2026-10-20 |
| Volksstimme | regional daily | active | 87 | 87 | 8.0% (7) | 0.0% (0) | 2026-10-20 |
| WAZ | regional daily | active | 80 | 80 | 8.8% (7) | 5.0% (4) | 2026-10-20 |
| WDR | public broadcaster regional | active | 34 | 34 | 20.6% (7) | 0.0% (0) | 2026-10-20 |
| Weser-Kurier | regional daily | active | 23 | 23 | 30.4% (7) | 0.0% (0) | 2026-10-20 |
| Wiesbadener Kurier | regional daily | active | 19 | 19 | 36.8% (7) | 0.0% (0) | 2026-10-20 |
| ZDFheute | public broadcaster national | active | 59 | 59 | 11.9% (7) | 10.2% (6) | 2026-10-20 |

</details>

<details>
<summary>Press-release sources: items per source</summary>

| Source | Type | Status | Items | Last 30 days | Annotated | With lead text | Latest publication |
|---|---|---|---|---|---|---|---|
| AfD | party | active | 1 | 1 | 100.0% (1) | 100.0% (1) | 2026-10-19 |
| AfD-Fraktion im Bundestag | parliamentary group | active | 8 | 8 | 100.0% (8) | 100.0% (8) | 2026-10-19 |
| Bayerische Staatsregierung | state government | active | 34 | 34 | 100.0% (34) | 100.0% (34) | 2026-10-19 |
| Bundesministerium der Justiz und für Verbraucherschutz | federal ministry | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Bundesministerium der Verteidigung | federal ministry | active | 5 | 5 | 100.0% (5) | 0.0% (0) | 2026-10-16 |
| Bundesministerium des Innern | federal ministry | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Bundesministerium für Bildung, Familie, Senioren, Frauen und Jugend | federal ministry | active | 2 | 2 | 100.0% (2) | 100.0% (2) | 2026-10-16 |
| Bundesministerium für Digitales und Staatsmodernisierung | federal ministry | active | 1 | 1 | 100.0% (1) | 100.0% (1) | 2026-10-16 |
| Bundesministerium für Gesundheit | federal ministry | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Bundesministerium für Umwelt, Klimaschutz, Naturschutz und nukleare Sicherheit | federal ministry | active | 3 | 3 | 100.0% (3) | 0.0% (0) | 2026-10-19 |
| Bundesministerium für wirtschaftliche Zusammenarbeit und Entwicklung | federal ministry | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Bundesministerium für Wohnen, Stadtentwicklung und Bauwesen | federal ministry | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Bundesrat | bundesrat | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Bundesregierung | federal government | active | 11 | 11 | 100.0% (11) | 100.0% (11) | 2026-10-19 |
| Bündnis 90/Die Grünen Bundestagsfraktion | parliamentary group | active | 5 | 5 | 100.0% (5) | 100.0% (5) | 2026-10-16 |
| Bündnis Sahra Wagenknecht | party | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| CDU | party | active | 3 | 3 | 100.0% (3) | 100.0% (3) | 2026-10-19 |
| CSU | party | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Deutscher Bundestag | bundestag | active | 37 | 37 | 100.0% (37) | 100.0% (37) | 2026-10-19 |
| Die Linke | party | active | 1 | 1 | 100.0% (1) | 100.0% (1) | 2026-10-17 |
| FDP | party | active | 9 | 9 | 100.0% (9) | 100.0% (9) | 2026-10-19 |
| Fraktion Die Linke im Bundestag | parliamentary group | active | 8 | 8 | 100.0% (8) | 100.0% (8) | 2026-10-19 |
| Niedersächsische Landesregierung | state government | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| Saarländische Landesregierung | state government | blocked | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| SPD | party | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |
| SPD-Bundestagsfraktion | parliamentary group | active | 0 | 0 | 0.0% (0) | 0.0% (0) | none |

</details>

**Annotations**

- **Annotated items:** 398 of 2,700 (14.7%)
- **Text seen by the model:** title_teaser 1,256, title_only 1,246, lead_text 198

**Main topics of political items, last 30 days** (359 items)

| Topic | Items | Share |
|---|---|---|
| Government operations | 87 | 24.2% |
| International affairs | 50 | 13.9% |
| Law and crime | 37 | 10.3% |
| Civil rights and liberties | 20 | 5.6% |
| Health | 19 | 5.3% |
| Macroeconomics | 18 | 5.0% |
| Technology | 17 | 4.7% |
| Social welfare | 15 | 4.2% |
| Defense | 13 | 3.6% |
| Environment | 13 | 3.6% |

**Fields** (Filled: records with a value that is not null and not empty)

<details>
<summary><code>polls</code>: 21 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (3,980) | 3,980 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (3,980) | 1 |
| `dataset` | Name of the dataset. | 100.0% (3,980) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (3,980) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (3,980) | 2 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (3,980) | 1 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (3,980) | 1 |
| `license` | License or terms of the source. | 100.0% (3,980) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (3,980) | 17 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (3,980) | 2 |
| `poll_id` | Id of the poll at DAWUM. | 100.0% (3,980) | 3,980 |
| `parliament` | Parliament the poll asks about. | 100.0% (3,980) | 3 |
| `institute` | Polling institute. | 100.0% (3,980) | 22 |
| `tasker` | Client that commissioned the poll. | 100.0% (3,980) | 119 |
| `method` | Survey method. | 100.0% (3,980) | 6 |
| `fieldwork_start` | First day of the fieldwork. | 100.0% (3,980) | 2,163 |
| `fieldwork_end` | Last day of the fieldwork. | 100.0% (3,980) | 2,208 |
| `published_date` | Publication date of the poll. | 100.0% (3,980) | 2,432 |
| `sample_size` | Number of respondents. | 100.0% (3,980) | 1,061 |
| `results` | Vote intention in percent by party code, without the others. | 100.0% (3,980) | 25 |
| `others_share` | Share of all other parties in percent. | 100.0% (3,980) | 64 |

</details>

<details>
<summary><code>wikipedia_pageviews</code>: 15 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (993) | 993 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (993) | 1 |
| `dataset` | Name of the dataset. | 100.0% (993) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (993) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (993) | 148 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (993) | 1 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (993) | 993 |
| `license` | License or terms of the source. | 100.0% (993) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (993) | 1 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (993) | 1 |
| `entity_id` | Party code, or politician id at abgeordnetenwatch. | 100.0% (993) | 993 |
| `entity_type` | Type of the entity. | 100.0% (993) | 2 |
| `wikipedia_title` | Title of the article in the German Wikipedia. | 100.0% (993) | 993 |
| `wikidata_qid` | Wikidata id of the entity. | 98.9% (982) | 982 |
| `daily_views` | Views by human users per day (UTC), for the days since the previous run. | 100.0% (993) | 4,128 |

</details>

<details>
<summary><code>news_items</code>: 30 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (2,572) | 2,572 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (2,572) | 1 |
| `dataset` | Name of the dataset. | 100.0% (2,572) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (2,572) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (2,572) | 66 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (2,572) | 40 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (2,572) | 2,572 |
| `license` | License or terms of the source. | 100.0% (2,572) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (2,572) | 17 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (2,572) | 1 |
| `outlet_id` | Id of the outlet. | 100.0% (2,572) | 40 |
| `outlet_type` | Type of the outlet. | 100.0% (2,572) | 8 |
| `owner_group` | Publisher group of the outlet, or 'public' for public broadcasters. | 100.0% (2,572) | 20 |
| `home_state` | State of a regional outlet; null for national outlets. | 53.9% (1,387) | 16 |
| `channel` | Where the article was found: RSS/Atom feed or news sitemap. | 100.0% (2,572) | 2 |
| `url` | URL of the article. | 100.0% (2,572) | 2,572 |
| `canonical_url` | Canonical URL declared by the article page. | 3.7% (94) | 94 |
| `title` | Title of the article. | 100.0% (2,572) | 2,180 |
| `published_at` | Publication time (UTC). | 99.9% (2,569) | 2,107 |
| `updated_at` | Last modification time from the article page (UTC). | 3.0% (77) | 70 |
| `section` | Section of the outlet, from the page, the feed or the URL path. | 37.7% (969) | 125 |
| `feed_categories` | Categories of the item in the feed. | 37.3% (959) | 599 |
| `keywords` | Keywords from the article page or the news sitemap. | 26.0% (670) | 2,077 |
| `jsonld_type` | schema.org type of the article page, e.g. NewsArticle or OpinionNewsArticle. | 3.1% (80) | 2 |
| `is_agency_copy` | True if the article is credited to a news agency (dpa, AFP, Reuters, epd, KNA, AP). Null without page data. | 3.1% (80) | 2 |
| `author_count` | Number of named authors. Author names are not stored. Null without page data. | 3.1% (80) | 5 |
| `word_count` | Word count from the article page. Null if not given. | 0.0% (0) | 0 |
| `is_paywalled` | True if the page marks the article as not accessible for free. Null without page data. | 2.2% (56) | 2 |
| `has_video` | True if the article page embeds a video. Null without page data. | 3.7% (95) | 2 |
| `content_basis` | Text available for the annotation: title only, title and teaser, or lead text of the page. | 100.0% (2,572) | 3 |

</details>

<details>
<summary><code>press_releases</code>: 19 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (128) | 128 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (128) | 1 |
| `dataset` | Name of the dataset. | 100.0% (128) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (128) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (128) | 105 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (128) | 14 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (128) | 128 |
| `license` | License or terms of the source. | 100.0% (128) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (128) | 2 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (128) | 1 |
| `press_source_id` | Id of the press source. | 100.0% (128) | 14 |
| `actor_type` | Type of the issuing actor. | 100.0% (128) | 6 |
| `party` | Party of a party or parliamentary group; null for other actors. | 27.3% (35) | 5 |
| `url` | URL of the press release. | 100.0% (128) | 128 |
| `canonical_url` | Canonical URL declared by the page. | 39.8% (51) | 51 |
| `title` | Title of the press release. | 100.0% (128) | 128 |
| `published_at` | Publication time (UTC). | 100.0% (128) | 98 |
| `feed_categories` | Categories of the item in the feed. | 29.7% (38) | 47 |
| `content_basis` | Text available for the annotation. | 100.0% (128) | 2 |

</details>

<details>
<summary><code>text_annotations</code>: 41 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (398) | 398 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (398) | 1 |
| `dataset` | Name of the dataset. | 100.0% (398) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (398) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (398) | 33 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (398) | 54 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (398) | 398 |
| `license` | License or terms of the source. | 100.0% (398) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (398) | 17 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (398) | 1 |
| `item_dataset` | Dataset of the annotated item. | 100.0% (398) | 2 |
| `item_id` | record_id of the annotated item. | 100.0% (398) | 398 |
| `model` | Gemini model that produced the labels. | 100.0% (398) | 1 |
| `extraction_hash` | Hash of the prompt, response schema and model settings. Labels with the same hash were produced under the same conditions. | 100.0% (398) | 1 |
| `content_basis` | Text the model saw: title only, title and teaser, or lead text. | 100.0% (398) | 3 |
| `is_political` | Whether the item is about politics. | 100.0% (398) | 2 |
| `political_level` | Main political level of the item. | 100.0% (398) | 6 |
| `states_concerned` | States the item is mainly about. | 28.9% (115) | 16 |
| `cap_topic` | Main policy topic (Comparative Agendas Project). | 100.0% (398) | 22 |
| `cap_topics_secondary` | Further policy topics. | 55.3% (220) | 20 |
| `issue_detail` | Specific issue in a few words. | 90.2% (359) | 300 |
| `article_type` | Form of the item. | 100.0% (398) | 7 |
| `event_types` | Types of political events reported. | 78.9% (314) | 19 |
| `event_status` | Stage of the main event. | 100.0% (398) | 4 |
| `government_involved` | Whether a government or its members act or are addressed. | 100.0% (398) | 2 |
| `mentioned_parties` | Parties the item refers to. | 41.0% (163) | 11 |
| `party_roles` | Role of each mentioned party. | 41.0% (163) | 11 |
| `party_tone` | Tone toward each mentioned party. | 41.0% (163) | 11 |
| `quoted_parties` | Parties whose representatives are quoted. | 11.1% (44) | 10 |
| `mentioned_politicians` | abgeordnetenwatch ids of the named politicians that are in the entity registry. | 26.1% (104) | 101 |
| `politician_tone` | Tone toward each resolved politician, by abgeordnetenwatch id. | 26.1% (104) | 101 |
| `unresolved_person_count` | Number of named persons that are not in the entity registry. Their names are not stored. | 100.0% (398) | 7 |
| `overall_tone` | Overall tone of the item. | 100.0% (398) | 4 |
| `government_tone` | Tone toward the federal government. | 100.0% (398) | 5 |
| `frame` | Dominant news frame (Semetko and Valkenburg). | 100.0% (398) | 6 |
| `conflict_pairs` | Pairs of parties reported in dispute, each pair sorted. | 8.3% (33) | 21 |
| `reports_poll` | Whether poll results are reported. | 100.0% (398) | 2 |
| `poll_institute` | Polling institute of a reported poll. | 2.0% (8) | 3 |
| `upcoming_events` | Future events with event_type and date (YYYY-MM-DD). | 13.3% (53) | 23 |
| `communication_type` | Purpose of a press release; 'Not applicable' for news. | 100.0% (398) | 8 |
| `populism_markers` | Populist rhetoric in a press release. | 2.5% (10) | 2 |

</details>

<details>
<summary><code>roll_call_votes</code>: 25 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (247) | 247 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (247) | 1 |
| `dataset` | Name of the dataset. | 100.0% (247) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (247) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (247) | 13 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (247) | 1 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (247) | 247 |
| `license` | License or terms of the source. | 100.0% (247) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (247) | 1 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (247) | 2 |
| `vote_id` | Id of the vote at abgeordnetenwatch. | 100.0% (247) | 247 |
| `parliament_period` | Electoral period, e.g. 'Bundestag 2025 - 2029'. | 100.0% (247) | 2 |
| `vote_date` | Date of the vote. | 100.0% (247) | 124 |
| `title` | Title of the vote. | 100.0% (247) | 240 |
| `topics` | Topics assigned by abgeordnetenwatch. | 99.2% (245) | 29 |
| `committees` | Committees in charge. | 79.8% (197) | 26 |
| `printed_papers` | Numbers of the Bundestag printed papers (Drucksachen) linked in the description of the vote, e.g. '21/7869'. They link the vote to procedures in the DIP (filter f.dokumentnummer). | 99.2% (245) | 419 |
| `accepted` | Whether the motion was accepted. | 100.0% (247) | 2 |
| `yes` | Number of yes votes. | 100.0% (247) | 180 |
| `no` | Number of no votes. | 100.0% (247) | 178 |
| `abstain` | Number of abstentions. | 100.0% (247) | 46 |
| `no_show` | Number of members who did not vote. | 100.0% (247) | 104 |
| `by_faction` | Counts of yes, no, abstain and no_show by parliamentary group at the time of the vote (party code, NONE for members without a group). | 100.0% (247) | 8 |
| `cohesion` | Rice index \|yes - no\| / (yes + no) per parliamentary group; null if the group cast no yes or no votes. Members without a group (NONE) are not included. | 100.0% (247) | 7 |
| `defectors` | Members per parliamentary group who voted yes while the group cast more no than yes votes, or no while it cast more yes than no votes. Members without a group (NONE) are not included. | 100.0% (247) | 7 |

</details>

<details>
<summary><code>mp_votes</code>: 16 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (172,677) | 172,677 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (172,677) | 1 |
| `dataset` | Name of the dataset. | 100.0% (172,677) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (172,677) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (172,677) | 16 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (172,677) | 1 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (172,677) | 172,677 |
| `license` | License or terms of the source. | 100.0% (172,677) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (172,677) | 1 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (172,677) | 2 |
| `vote_id` | Id of the vote at abgeordnetenwatch. | 100.0% (172,677) | 247 |
| `vote_date` | Date of the vote. | 100.0% (172,677) | 124 |
| `politician_id` | Id of the politician at abgeordnetenwatch. | 100.0% (172,677) | 1,008 |
| `faction` | Parliamentary group at the time of the vote as party code, NONE for members without a group. | 100.0% (172,677) | 8 |
| `vote` | The member's vote. | 100.0% (172,677) | 4 |
| `voted_against_faction` | True if the member voted yes while the group cast more no than yes votes, or no while it cast more yes than no votes. Always false for members without a group. | 100.0% (172,677) | 2 |

</details>

<details>
<summary><code>parliamentary_procedures</code>: 24 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (19,265) | 19,265 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (19,265) | 1 |
| `dataset` | Name of the dataset. | 100.0% (19,265) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (19,265) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (19,265) | 21 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (19,265) | 1 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (19,265) | 19,265 |
| `license` | License or terms of the source. | 100.0% (19,265) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (19,265) | 1 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (19,265) | 1 |
| `procedure_id` | Id of the procedure in the DIP. | 100.0% (19,265) | 19,265 |
| `change` | new for the first observation, status_changed if the status changed, updated for other tracked changes. | 100.0% (19,265) | 1 |
| `procedure_type` | Type of the procedure, e.g. Gesetzgebung, Antrag, Kleine Anfrage. | 100.0% (19,265) | 43 |
| `electoral_period` | Electoral period of the Bundestag. | 100.0% (19,265) | 1 |
| `title` | Title of the procedure. | 100.0% (19,265) | 18,966 |
| `initiators` | Initiators, e.g. Bundesregierung, Bundesrat or a parliamentary group. | 39.8% (7,666) | 85 |
| `subject_areas` | Subject areas (Sachgebiete) of the DIP. | 26.2% (5,055) | 28 |
| `descriptors` | Subject descriptors of the DIP thesaurus. | 88.6% (17,077) | 7,907 |
| `consent_required` | Whether the consent of the Bundesrat is required, from the latest statement of the DIP (bill, Bundestag decision or promulgation); null if not stated. | 2.3% (450) | 2 |
| `status` | Current status (Beratungsstand). | 83.6% (16,104) | 25 |
| `previous_status` | Status at the previous observation; null for new procedures. | 0.0% (0) | 0 |
| `gesta_id` | GESTA id of a bill. | 2.3% (450) | 450 |
| `dip_updated_at` | Last update of the procedure in the DIP (UTC). | 100.0% (19,265) | 8,296 |
| `latest_document_date` | Date of the latest document of the procedure (DIP field datum), e.g. of the printed paper or plenary record that set the current status. | 100.0% (19,265) | 383 |

</details>

<details>
<summary><code>politicians</code>: 17 fields</summary>

| Field | Description | Filled | Distinct values |
|---|---|---|---|
| `record_id` | Stable id of the item. Together with revision it identifies a record. | 100.0% (2,513) | 2,513 |
| `revision` | 0 for the first observation; incremented each time the source changes the item. | 100.0% (2,513) | 1 |
| `dataset` | Name of the dataset. | 100.0% (2,513) | 1 |
| `schema_version` | Version of the record schema. | 100.0% (2,513) | 1 |
| `observed_at` | UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t. | 100.0% (2,513) | 1 |
| `source_id` | Id of the source (API, outlet or press source). | 100.0% (2,513) | 1 |
| `source_url` | URL of the item or of the API resource it was read from. | 100.0% (2,513) | 2,513 |
| `license` | License or terms of the source. | 100.0% (2,513) | 1 |
| `jurisdiction` | DE for the federal level, ISO 3166-2 code for a state. | 100.0% (2,513) | 17 |
| `backfilled` | True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public. | 100.0% (2,513) | 1 |
| `politician_id` | Id of the politician at abgeordnetenwatch. | 100.0% (2,513) | 2,513 |
| `name` | Full name. | 100.0% (2,513) | 2,507 |
| `party` | Current party as party code; NONE without party; OTHER for parties not in the registry; null if unknown. | 100.0% (2,513) | 12 |
| `sex` | Sex as stated by abgeordnetenwatch. | 99.6% (2,504) | 3 |
| `year_of_birth` | Year of birth. | 87.8% (2,207) | 59 |
| `wikidata_qid` | Wikidata id. | 39.1% (982) | 982 |
| `mandates` | Current mandates with parliament, period, faction (party code, NONE without group), constituency and electoral_list the member stood in (null if none), mandate_won (constituency, list or moved_up), start_date and end_date (YYYY-MM-DD; null if equal to the start or end of the period). | 100.0% (2,513) | 2,335 |

</details>
<!-- DATASET_OVERVIEW_END -->

## Datasets

| Dataset | Content | Source | Kind |
|---|---|---|---|
| `polls` | Opinion polls (Sonntagsfrage) for the Bundestag, the 16 state parliaments and the European Parliament, since 2017 | DAWUM | event |
| `wikipedia_pageviews` | Daily views of the German Wikipedia articles of parties (since July 2015) and members of parliament | Wikimedia | event |
| `news_items` | Metadata of political news articles of German outlets | RSS feeds, news sitemaps, article pages | event |
| `press_releases` | Metadata of press releases of the federal government, ministries, parliaments, parliamentary groups, parties and courts | RSS feeds | event |
| `text_annotations` | Gemini labels of up to 400 news articles and press releases per run (press releases first): topic, event types, parties and politicians with role and tone, frame, conflicts, polls, upcoming events | Gemini | event |
| `roll_call_votes` | Roll-call votes of the Bundestag with counts, cohesion and defectors per parliamentary group, and the printed papers (Drucksachen) they refer to | abgeordnetenwatch.de | event |
| `mp_votes` | The vote of each member in each roll-call vote | abgeordnetenwatch.de | event |
| `parliamentary_procedures` | Procedures of the Bundestag and Bundesrat (bills, motions, interpellations), recorded when they appear or change | Bundestag DIP | change |
| `politicians` | Members of the Bundestag and the 16 state parliaments with party, mandates and Wikidata id, recorded when they appear or change | abgeordnetenwatch.de | change |

**Fields of every record:** `record_id`, `revision`, `dataset`, `schema_version`, `observed_at`, `source_id`, `source_url`, `license`, `jurisdiction`, `backfilled`. All other fields are defined with descriptions in [src/schema.py](src/schema.py); the variables and the prediction targets they serve are described in [project_architecture.md](project_architecture.md).

**Point-in-time correctness.** `observed_at` is the time at which the pipeline observed the item. Filtering `observed_at <= t` gives the state of knowledge at time t, so that models can be tested on past data without information from the future. Records of items that are older than the start of collection (e.g. the poll history) have `backfilled: true`.

**Revisions.** Event datasets store each item once. If a source changes an item (e.g. DAWUM corrects a poll), a new record with the same `record_id` and the next `revision` is written. Change datasets write a record for each observed change. The valid version at time t is the record with the highest revision observed at or before t.

**No full texts.** News articles and press releases are stored as titles, URLs, metadata and derived variables. The lead text that Gemini reads is not stored.

**Annotated sample.** Every new political item of a run is stored in `news_items` or `press_releases`. Up to 400 of them are annotated: all press releases, then news items in turns across the outlets, newest first. The other items are stored from the feed data without labels, so that the item counts per outlet stay complete and the annotated sample can be weighted against them.

## Data Model

The diagram shows the nine datasets, the three reference lists they point to, and the fields that link them. `party`, `outlet` and `press_source` are not datasets: the party codes are defined in [src/entities.py](src/entities.py), the outlets and press-release sources in [config/sources.toml](config/sources.toml), with the dates `added` and `removed` of their coverage period. Solid lines are joins on stored fields; the dashed line needs a lookup in the DIP API.

```mermaid
erDiagram
    party {
        string code PK "e.g. CDU_CSU, SPD, GRUENE"
    }
    outlet {
        string id PK
        string outlet_type
        string home_state
        date added
        date removed
    }
    press_source {
        string id PK
        string actor_type
        date added
        date removed
    }
    polls {
        string poll_id PK
        string parliament
        date published_date
        map results "vote share by party code"
    }
    politicians {
        string politician_id PK "abgeordnetenwatch id"
        string party FK
        string wikidata_qid
        list mandates
    }
    roll_call_votes {
        string vote_id PK
        date vote_date
        list printed_papers "Drucksache numbers"
        bool accepted
    }
    mp_votes {
        string vote_id FK
        string politician_id FK
        string faction FK "party code or NONE"
        string vote
    }
    parliamentary_procedures {
        string procedure_id PK "DIP id"
        string procedure_type
        string status
        string change
    }
    wikipedia_pageviews {
        string entity_id FK "party code or politician_id"
        string entity_type "party or politician"
        map daily_views
    }
    news_items {
        string record_id PK
        string outlet_id FK
        string url
        datetime published_at
    }
    press_releases {
        string record_id PK
        string press_source_id FK
        string party FK
        datetime published_at
    }
    text_annotations {
        string item_id FK "record_id of the item"
        string item_dataset "news_items or press_releases"
        string extraction_hash
        list mentioned_parties FK
        list mentioned_politicians FK
    }

    party |o--o{ politicians : "party"
    party |o--o{ mp_votes : "faction"
    party }o--o{ polls : "results keys"
    party |o--o{ wikipedia_pageviews : "entity_id"
    party |o--o{ press_releases : "party"
    party }o--o{ text_annotations : "mentioned_parties"
    outlet ||--o{ news_items : "outlet_id"
    press_source ||--o{ press_releases : "press_source_id"
    roll_call_votes ||--|{ mp_votes : "vote_id"
    politicians |o--o{ mp_votes : "politician_id"
    politicians |o--o{ wikipedia_pageviews : "entity_id"
    politicians }o--o{ text_annotations : "mentioned_politicians"
    news_items |o--o{ text_annotations : "item_id"
    press_releases |o--o{ text_annotations : "item_id"
    roll_call_votes }o..o{ parliamentary_procedures : "printed_papers via DIP"
```

**Reading the joins:**
- Every record also has the common fields (`record_id`, `revision`, `observed_at`, ...). `politicians`, `parliamentary_procedures` and revised items have several records per key; for a point in time t, use the record with the highest revision observed at or before t.
- `politicians` holds the members with a current mandate. Members of the previous Bundestag who left have votes in `mp_votes` but no entry in `politicians`.
- An item has one annotation per `extraction_hash`, and none if it was stored without labels.
- `mentioned_politicians` holds only names that resolved to the registry; the others are counted in `unresolved_person_count`.
- The DIP API finds the procedure of a printed paper with the filter `f.dokumentnummer`.

## Sources and Fetch Rules

- **APIs:** DAWUM (polls), abgeordnetenwatch.de API v2 (politicians, roll-call votes; at most 30 requests per minute), Bundestag DIP API (procedures; API key), Wikimedia Pageviews API, MediaWiki and Wikidata APIs (article titles). These documented APIs are used under their API terms.
- **Websites:** the news outlets and press-release sources in [config/sources.toml](config/sources.toml), read through RSS/Atom feeds, news sitemaps and, where permitted, the article pages.
- **robots.txt:** every website request is checked against the robots.txt of its host for the user agent `GermanPoliticsDataset`, with the longest-match rule of RFC 9309. A crawl delay is honored; a host that requires more than 120 seconds is not fetched. A host whose robots.txt cannot be fetched is not requested again in the same run.
- **Bot protection:** a source that answers with HTTP 401/403, a challenge page or a CAPTCHA is set to `blocked` and is not fetched again until it is reset by hand. The pipeline does not use a headless browser.
- **Text and data mining reservations:** a source whose host or pages declare a machine-readable reservation (TDMRep header, `/.well-known/tdmrep.json` or meta tag) is set to `tdm_reserved` and is not used. For outlets whose robots.txt states a reservation under § 44b UrhG in plain text, article pages are not fetched (`fetch_pages = false`); their feed items are used with title and teaser only.
- **Pacing:** at most one request per second per host (abgeordnetenwatch: one per 2.1 seconds), retries for server errors and rate limits.
- **Broken sources:** a source whose feeds fail on 7 consecutive runs is `broken` and is retried weekly.
- **Source review:** on the first day of every month, `source_review.yml` checks the list and proposes removals, fixes and additions as a pull request (see [Workflows](#workflows)).

## Pipeline

```mermaid
sequenceDiagram
    participant Action as GitHub Actions
    participant Run as main.py / pipeline.py
    participant AW as abgeordnetenwatch
    participant DIP as Bundestag DIP
    participant DAWUM as DAWUM
    participant Wiki as Wikimedia / Wikidata
    participant Web as Feeds and pages
    participant Gemini as Gemini API
    participant Store as storage.py / state.py

    Action->>Run: Pipeline run (Tuesdays and Fridays, 02:07 Europe/Berlin)
    Run->>AW: Members of the Bundestag and the state parliaments (politician registry, once a week)
    Run->>Web: Feeds and news sitemaps of all due sources
    loop Up to 400 new items (press releases first), in turns across the sources
        Run->>Web: Article page (metadata, lead text), where permitted
        Run->>Gemini: Batches of 10 items, up to 4 at the same time
        Gemini-->>Run: Labels per item
        Run->>Store: Items and annotations (every 100 items)
    end
    Run->>Store: Further new items without labels
    Run->>AW: New roll-call votes and the individual votes
    Run->>DIP: Procedures updated since the previous run
    Run->>DAWUM: Poll database, if it changed
    Run->>Wiki: Daily views since the last stored day (once a week)
    Action->>Action: Update the README overview, commit data/
```

The feeds run right after the politician registry, because news items leave the feeds after a few days; the data of the later collectors can also be fetched by a later run. `politicians` and `wikipedia_pageviews` run once a week, six days or more after their last complete run; pageviews are fetched for all days since the last stored day, so no day is lost.

**Run deadline.** The collectors start no new work 25 minutes after the start of the run (`RUN_DEADLINE_MINUTES`), so that the workflow commits the data before its timeout of 30 minutes. A collector that stops at the deadline has the status `partial` and continues in the next run; a collector that has not started by then is skipped. Items of the feeds that are not annotated by the deadline are stored without labels.

Each collector runs in isolation: a failure is logged in `data/state/collectors.json` and the run log, the other collectors continue, and the data already written is kept. The run then ends with exit code 1, so that the workflow fails visibly.

## Data Files

| Path | Content |
|---|---|
| `data/<dataset>/year=YYYY/month=MM/<dataset>_YYYY-MM-DD.json` | Records of a dataset observed on one UTC day |
| `data/state/seen/<dataset>.json` | Items already written, with content hash and revision |
| `data/state/collectors.json` | First run, last run, failures and cursor per collector |
| `data/state/sources.json` | Health status of the news and press-release sources |
| `data/state/politicians.json` | Politician registry used to resolve names in the annotations |
| `data/state/run_log.jsonl` | One line per run with the outcome and counters of each collector |
| `data/LICENSE.md` | License and attribution of the data |

Each data file is a JSON list with one record per line. The folder names `year=` and `month=` let DuckDB, Polars and pyarrow read a dataset as one table:

```python
import duckdb
polls = duckdb.sql("select * from read_json('data/polls/**/*.json', hive_partitioning=true)").df()
```

## Workflows

| Workflow | Schedule | Purpose |
|---|---|---|
| `pipeline.yml` | Tuesdays and Fridays, 02:07 Europe/Berlin | Runs the due collectors (timeout 30 minutes), updates the overview and commits `data/` and `README.md` |
| `notify.yml` | After each `normal` or `backfill` run of `pipeline.yml` | Posts a summary of the run (status and records per collector, annotated items, Gemini tokens and estimated cost) as a commit comment that mentions the repository owner |
| `source_review.yml` | First day of every month, 07:17 Europe/Berlin | Checks the sources and proposes changes of `config/sources.toml` as a pull request |
| `ci.yml` | Pull requests, pushes to `main` | Lint, tests and commit message checks |

**Notification email.** GitHub sends an email for the mention in the comment of `notify.yml` if email notifications for participating and @mentions are enabled (the default, under **Settings > Notifications** of the GitHub account). The summary is built by `scripts/run_summary.py` from the last line of `data/state/run_log.jsonl`; the cost estimate uses the list prices in that script. `notify.yml` runs only once it is on the default branch.

**Source review.** `source_review.yml` (code in [src/source_review.py](src/source_review.py)) proposes changes of `config/sources.toml` as a pull request from the branch `source-review`; they take effect when the pull request is merged. The description lists every change with its reason.
- **Removals:** sources that are `blocked` or `tdm_reserved`, that are `broken` and still fail, that have no working feed left, whose robots.txt requires a crawl delay above 120 seconds, or that yielded no political item in 90 days. A removed source keeps its entry with `removed` and `removed_reason`, so that its coverage period stays known. To keep a source with low yield, set `keep = true` in its entry.
- **Fixes:** feeds that return HTTP 404 or 410, are disallowed by robots.txt or whose newest item is older than 180 days are removed; feeds that moved permanently get their new URL. Page fetching is turned off where robots.txt states a TDM reservation in natural language or requires a crawl delay above 10 seconds.
- **Additions:** Gemini with Google Search looks for the official press-release feeds of the federal government and its ministries, of Bundestag groups and parties with seats that have no press source and of state governments without one, and for regional dailies in states without one. Each candidate is checked with the fetch rules of the pipeline: readable robots.txt, no TDM reservation, no bot protection, and recent items (a press release in the last 60 days, or five political articles in the last 7 days). At most five sources are added per review, with `added` set to the review date.
- **Exclusions:** rejected candidates are added to the `[[excluded]]` entries, so that the search does not propose them again. Blocked and TDM-reserved candidates stay excluded until the entry is changed by hand; candidates without a usable feed are checked again after a year.

Hosts that blocked automated requests or reserved TDM are not requested again. The review sends about four Gemini requests with Google Search per month, which cost a few cents. A pull request opened with the `GITHUB_TOKEN` starts no workflows, so the review starts `ci.yml` on its branch. The next review rebuilds the branch, so merge or close the pull request before the first day of the next month. A manual run can turn the search off.

**Manual runs.** The workflows can be started by hand: open the **Actions** tab, select **German Politics Pipeline** (or **Source Review**, **CI**), click **Run workflow**, and choose the branch and the inputs. The button appears once the workflow file is on the default branch. The modes of `pipeline.yml`:
- `normal`: same as the scheduled run; commits the data.
- `backfill`: imports the history of all collectors except the feeds, without the per-run caps and the weekly interval, and commits it (timeout 90 minutes, deadline 85 minutes). Run it once before the first `normal` run (see [Setup](#setup)).
- `shadow`: runs on a copy of the state in a temporary folder and uploads the output as an artifact; nothing is committed. Uses the same API keys and quotas.
- `dry-run`: runs the collectors without writing any data; the feeds collector reads the feeds and sitemaps only (no article pages, no Gemini calls). The workflow log lists the new items per source and the counts.

The input `collectors` restricts a manual run to a comma-separated list of collectors, e.g. `feeds`; selected collectors run even if they are not due. For a first test, start with `dry-run` and `collectors` set to `feeds` (checks access to the sources and the number of new items per source), then `backfill`, then `shadow` (checks the API keys and a regular run without committing), then `normal`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | none | Gemini API key (required for news and press releases) |
| `DIP_API_KEY` | none | Bundestag DIP API key (required for the procedures) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for the annotation; stored in every annotation record |
| `MAX_GEMINI_CALLS_PER_RUN` | 50 | Gemini requests per run, including retries |
| `MAX_ANNOTATIONS_PER_RUN` | 400 | News articles and press releases annotated per run; further new items are stored without labels |
| `GEMINI_CONCURRENCY` | 4 | Gemini requests sent at the same time; 1 for the free tier of the Gemini API |
| `NEWS_LOOKBACK_HOURS` | 96 | Age limit of new feed and sitemap items |
| `NEWS_FETCH_PAGES` | on | Fetch article pages for metadata and lead text |
| `RUN_DEADLINE_MINUTES` | 25 | Minutes after the start after which the collectors start no new work (85 in `backfill` runs) |
| `COLLECTORS` | all due | Comma-separated list of collectors to run; listed collectors run even if they are not due |

Fixed thresholds, such as the source status rules, are constants in the modules that use them.

## Running Locally

```bash
uv sync
uv run python src/main.py --dry-run --collectors feeds          # read the feeds only
uv run python src/main.py --backfill                             # import the history (no feeds)
uv run python src/main.py --collectors polls,wikipedia_pageviews # selected collectors
uv run python src/main.py --max-annotations 20                   # a short full run
uv run python src/generate_dataset_overview.py                   # README overview
uv run python src/source_review.py --report review.md --no-search # source review; changes config/sources.toml
```

Secrets are read from a `.env` file in the repository root (`GEMINI_API_KEY=...`, `DIP_API_KEY=...`); the file is listed in `.gitignore`. Local runs write to `~/Downloads/german_politics_dataset/` unless `--output-dir` is given; in GitHub Actions they write to `data/`. The collectors, in the order of a run, are `politicians`, `feeds`, `roll_call_votes`, `parliamentary_procedures`, `polls` and `wikipedia_pageviews`.

## Continuous Integration

The `CI` workflow runs on every pull request and on pushes to `main`:

| Job | Checks |
|---|---|
| `Lint` | `uv.lock` matches `pyproject.toml`; `ruff check`; `actionlint` (including `shellcheck`) on the workflow files |
| `Tests` | The tests in `tests/` on Python 3.11 and 3.14. They run offline, with a fake web and a fake Gemini client, and check that every module imports with the locked dependencies. |
| `Conventional Commits` | Every commit of a pull request follows the Conventional Commits format (pull requests only) |
| `CI passed` | Succeeds only if all jobs above succeeded or were skipped; use it as the required status check of `main` |

Dependencies are managed with [uv](https://docs.astral.sh/uv/): `pyproject.toml` declares them, `uv.lock` pins them, and Dependabot proposes weekly updates. Run the checks locally before pushing:

```bash
uv lock --check
uv run ruff check .
uv run pytest
```

## Setup

1. Add the repository secrets `GEMINI_API_KEY` and `DIP_API_KEY` under **Settings > Secrets and variables > Actions**. The current public DIP key is listed on the [DIP help page](https://dip.bundestag.de/%C3%BCber-dip/hilfe/api); it is replaced from time to time.
2. Under **Settings > Actions > General > Workflow permissions**, choose read and write permissions, so that the workflows can push, and allow GitHub Actions to create and approve pull requests, so that the source review can open its pull request.
3. Start `pipeline.yml` once by hand in the mode `backfill` before the first scheduled run. It imports the history: all polls since 2017, the pageviews of the party articles since July 2015 and of the members of parliament for one year, all procedures of the current electoral period, and the roll-call votes of the current and the previous electoral period (about 250, at the rate limit of abgeordnetenwatch). It takes about 40 minutes. The regular runs then fetch only the changes; without the backfill, they import the history over several runs, at most 120 roll-call votes per run and within the run deadline.

## License

- **Code:** MIT License, see [LICENSE](LICENSE).
- **Data** (`data/`): Open Database License (ODbL) 1.0, individual contents under the Database Contents License (DbCL) 1.0, see [data/LICENSE.md](data/LICENSE.md). The poll data comes from [DAWUM](https://dawum.de/) under the ODbL. Titles and URLs of news articles and press releases remain subject to the rights of their publishers.
