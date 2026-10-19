# Data License

This license applies to the datasets in the `data/` folder of this repository. The code of the repository is licensed under the MIT License (see [LICENSE](../LICENSE) in the repository root).

## Table of Contents
- [License](#license)
- [Attribution](#attribution)
- [Third-Party Content](#third-party-content)
- [Obligations of Reusers](#obligations-of-reusers)

## License

The German Politics Dataset is made available under the Open Database License: https://opendatacommons.org/licenses/odbl/1-0/. Any rights in individual contents of the database are licensed under the Database Contents License: https://opendatacommons.org/licenses/dbcl/1-0/.

Copyright (c) 2026 Conrad Kleinn.

## Attribution

The datasets contain data from the following sources. The field `license` of every record names the license or terms of its source.

| Source | Datasets | License or terms |
|---|---|---|
| DAWUM (https://dawum.de/) | `polls` | Open Database License (ODbL) 1.0. Contains information from DAWUM, which is made available under the ODbL. |
| abgeordnetenwatch.de (https://www.abgeordnetenwatch.de/) | `politicians`, `roll_call_votes`, `mp_votes` | CC0 1.0 |
| Deutscher Bundestag, DIP (https://dip.bundestag.de/) | `parliamentary_procedures` | Terms of use of the DIP |
| Wikimedia Foundation, Pageviews API (https://wikimedia.org/api/rest_v1/) | `wikipedia_pageviews` | CC0 1.0 |
| Wikidata (https://www.wikidata.org/) | article titles of `wikipedia_pageviews` | CC0 1.0 |
| News outlets and press-release issuers listed in `config/sources.toml` | `news_items`, `press_releases` | Rights of the publishers; see [Third-Party Content](#third-party-content) |

The labels in `text_annotations` were produced with Google Gemini by this project.

## Third-Party Content

`news_items` and `press_releases` contain titles, URLs and metadata of articles and press releases. The texts of the articles and press releases are not stored. Titles and URLs remain subject to the rights of their publishers; the licenses above cover the selection, the structure and the derived variables of this database, not these contents.

## Obligations of Reusers

In summary, and without replacing the license text:

- **Attribution:** state that the data comes from this dataset and keep the attribution of DAWUM for poll data.
- **Share-alike:** a database that is derived from this dataset and used publicly must be published under the ODbL.
- **Produced works:** charts, model predictions or articles created from the data require a notice that names the dataset and the ODbL, but do not have to be published under the ODbL.
- **No technical restrictions:** a derived database that is published with technical restrictions must also be offered without them.
