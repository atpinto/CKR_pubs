# CKR publication register

This repository contains a static publication register with an automatic weekly PubMed update.

```text
repository root/
├── .github/
│   └── workflows/
│       └── update-publications.yml
├── scripts/
│   └── update_publications.py
├── getsitelogo.jpeg
├── publications.html
└── publications.csv
```

Keep the files in these exact locations. Publish `publications.html` and `publications.csv` with GitHub Pages.

## Enable the weekly update

1. Upload all four files and commit them to the repository’s default branch.
2. Open the repository’s **Actions** tab.
3. Select **Update publications from PubMed**.
4. Select **Run workflow** to test it once.
5. Confirm that the run commits a refreshed `publications.csv`.

The workflow then runs every Monday at 7:00 am in the `Australia/Sydney` timezone. It can also be run manually from the Actions tab.

The workflow uses only Python’s standard library. An NCBI API key is optional. If desired, add it as a repository Actions secret named `NCBI_API_KEY` and add a contact email as a repository variable named `NCBI_EMAIL`.

If GitHub rejects the automatic commit, open **Settings → Actions → General → Workflow permissions** and allow workflows to write repository contents. Organisation policies or branch protection can still prevent a direct push; in that case, allow the Actions bot to update the branch or change the workflow to create a pull request.

## What the updater preserves

The Python updater:

- refreshes new and existing PubMed records;
- preserves project and programme values marked with `projects_source=manual` or `programmes_source=manual`;
- retains manually added records that have no PMID;
- retains previously included PubMed records with an affiliation-review flag if their CKR affiliation can no longer be verified;
- writes the CSV atomically only after the complete PubMed download validates successfully.

## Using the webpage

The webpage loads the latest committed `publications.csv` whenever it opens. Select **Reload latest CSV** after a workflow finishes if the page is already open.

Use **Add publication** or **Edit record & associations** for browser-based manual changes. These manual edits still need to be downloaded and committed because a public webpage cannot securely push to GitHub without authentication.

## Editing the CSV directly

The `projects` and `programmes` columns are intended for direct editing. Separate multiple names with ` | `. Multi-value citation columns such as `authors` and `affiliations` contain JSON arrays so commas and semicolons round-trip safely.

Save the file as UTF-8 CSV and keep the header row unchanged.

## Local testing

Run the updater from the repository root:

```bash
python scripts/update_publications.py --csv publications.csv
```

If `publications.html` is opened directly from a computer, select **Open CSV file** and choose `publications.csv`. Alternatively, serve the folder with any local web server.
