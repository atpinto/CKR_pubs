# CKR publication register

The Centre for Kidney Research publication register uses a public Supabase PostgreSQL database, a static GitHub Pages interface, and a weekly PubMed synchronization.

```text
repository root/
├── .github/workflows/update-publications.yml
├── scripts/
│   ├── update_publications.py
│   ├── sync_database.py
│   ├── scholar_citations.py
│   └── test_scholar_citations.py
├── supabase/schema.sql
├── config.js
├── getsitelogo.jpeg
├── index.html
├── publications.html
└── publications.csv
```

The CSV is a portable backup. The database is the live source used by the website.

## 1. Create and secure the Supabase database

1. Create a Supabase project in the Sydney region.
2. Open **SQL Editor**, paste `supabase/schema.sql`, and run it once.
3. In **Authentication → Users**, create each editor account with an email and password.
4. Approve each account by running this in the SQL Editor, replacing the email:

```sql
insert into private.publication_editors (user_id, label)
select id, email from auth.users where email = 'editor@example.org'
on conflict (user_id) do update set label = excluded.label;
```

The security policies allow everyone to read publications. Only users listed in `private.publication_editors` may add, update, or delete records. Every database change is written to `public.publication_history`.

## 2. Configure the website

Open the Supabase project’s **Connect** dialog and copy its project URL and publishable key into `config.js`:

```js
window.CKR_CONFIG = Object.freeze({
  supabaseUrl: 'https://PROJECT.supabase.co',
  supabasePublishableKey: 'PUBLISHABLE_KEY'
});
```

The publishable key is intended for browser use and is constrained by Row Level Security. Never put the Supabase secret key or legacy service-role key in `config.js`, HTML, or any committed file.

## 3. Import the current CSV once

After adding the GitHub secrets in the next section, open **Actions → Update publications from PubMed → Run workflow**, select **Import publications.csv without querying PubMed**, and run it. This is the simplest first import.

Alternatively, keep the secret key in an environment variable and seed from the repository root:

```bash
SUPABASE_URL='https://PROJECT.supabase.co' \
SUPABASE_SECRET_KEY='SECRET_KEY' \
python scripts/sync_database.py --csv publications.csv --seed
```

This imports the current records without querying PubMed.

## 4. Enable the weekly update

In **GitHub → CKR_pubs → Settings → Secrets and variables → Actions**, add:

- `SUPABASE_URL` as a repository secret;
- `SUPABASE_SECRET_KEY` as a repository secret;
- `NCBI_API_KEY` as an optional repository secret;
- `NCBI_EMAIL` as an optional repository variable;
- `SERPAPI_API_KEY` as an optional repository secret (see section 5);
- `CITATION_MAX_LOOKUPS` and `CITATION_TIME_LIMIT_MINUTES` as optional repository variables (see section 5).

For the first run, select **Import publications.csv without querying PubMed**. On later manual runs, leave it unselected so PubMed is refreshed normally.

The workflow runs every Monday at 7:00 am in the `Australia/Sydney` timezone. It downloads the live database first, refreshes PubMed fields, preserves associations marked as manual, writes the result back to Supabase, and then refreshes Google Scholar citation counts (section 5). It commits `publications.csv` as a backup.

If the workflow cannot commit the CSV, open **Settings → Actions → General → Workflow permissions** and allow workflows to write repository contents.

## 5. Citation counts (Google Scholar)

Each publication shows a **Cited by** count from Google Scholar. The weekly workflow looks the counts up in the same run that refreshes PubMed, after the PubMed update has been saved. Citation lookups are best effort: if they fail, the PubMed update, the database and the CSV backup are unaffected, and existing counts are kept.

**One-time database change.** An existing database needs two new columns. In **SQL Editor**, run `supabase/schema.sql` again (it is safe to re-run), or run only this:

```sql
alter table public.publications
  add column if not exists citations integer
    constraint publications_citations_nonnegative check (citations is null or citations >= 0),
  add column if not exists citations_checked_at timestamptz;
```

Until this is run, the website and the weekly update work exactly as before, and the workflow log shows a warning that citation counts were skipped.

**Google Scholar has no official API,** so there are two ways to read it:

- **Direct (default, free).** The script searches scholar.google.com by title. Google restricts automated access and often blocks cloud servers such as GitHub Actions. When Google asks for a CAPTCHA, the script stops for that run and keeps the existing counts.
- **SerpApi (more reliable, paid).** Add a `SERPAPI_API_KEY` repository secret and the script uses [SerpApi](https://serpapi.com/google-scholar-api) instead. Each lookup uses one SerpApi search, so check their current plans and set `CITATION_MAX_LOOKUPS` to fit.

**How many records per run.** A run looks up at most 100 records (`CITATION_MAX_LOOKUPS`) and stops after 60 minutes (`CITATION_TIME_LIMIT_MINUTES`). Records that have never been checked go first, newest first, then the records with the oldest check, so the whole collection is covered over several weeks. At 100 per run, a first pass over about 1,350 records takes roughly 14 weekly runs. For a faster first pass, run the workflow manually with a larger **Google Scholar lookups** value; this is only realistic with SerpApi.

**Trying it.** Select **Actions → Update publications from PubMed → Run workflow**, set **Google Scholar lookups** to `10`, and read the `Citation counts (…)` line in the log. If the log warns that Google Scholar asked for a CAPTCHA or refused the request (HTTP 403), direct mode will not work from GitHub Actions; use SerpApi or run the lookups on your own computer (next section). Tick **Skip the Google Scholar citation refresh** to run the PubMed update alone.

### Running the citation lookups on your own computer

Google Scholar usually accepts requests from an ordinary home or office connection even when it blocks GitHub's servers. `--citations-only` looks up counts and saves them to the database, and does nothing else: no PubMed update, and `publications.csv` is not touched. The next weekly run copies the counts into the CSV backup.

You need a recent Python 3 (the weekly workflow uses 3.13; this was tested on 3.11) and a copy of this repository. In Supabase, open **Project Settings → API Keys** and copy the **secret key** (not the publishable key). Treat it like a password: keep it out of any file in the repository and out of chat messages.

macOS or Linux:

```bash
export SUPABASE_URL='https://PROJECT.supabase.co'
export SUPABASE_SECRET_KEY='SECRET_KEY'
python3 scripts/sync_database.py --citations-only --citation-max-lookups 50
```

Windows PowerShell:

```powershell
$env:SUPABASE_URL = 'https://PROJECT.supabase.co'
$env:SUPABASE_SECRET_KEY = 'SECRET_KEY'
python scripts/sync_database.py --citations-only --citation-max-lookups 50
```

The values last only for that terminal window. Each lookup prints a line as it is saved, with a pause of about 10 seconds between lookups, so 50 records take roughly 10 minutes. The script stops by itself if Google asks for a CAPTCHA, and records already saved are kept.

- Start with 30 to 50 records. If Google blocks you, wait a few hours, or until the next day, before trying again, and do not run several copies at once.
- Run it again later to continue. Each run picks up the records that have never been checked, then the stalest counts.
- `--citation-max-lookups` sets how many records to look up. To go slower, set `CITATION_DELAY_SECONDS` to a larger number such as `20`.
- If you also add a `SERPAPI_API_KEY`, the same command uses SerpApi instead of contacting Google directly.
- To stop the weekly GitHub run from trying (and warning about) Google Scholar, add a repository variable `CITATION_MAX_LOOKUPS` set to `0`.

**Matching.** A count is saved only when one of the top Google Scholar results has the same title as the record (ignoring case, punctuation and accents). If none match, the record is marked as checked without a count and tried again in a later cycle. Counts can differ from other databases, and an edited title may need a later refresh.

### Scopus citation counts

Scopus counts are shown next to the Google Scholar counts as "Cited by N in Scopus", linking to Scopus. They come from Elsevier's official Scopus Search API, so there is no CAPTCHA, and one request covers 25 records: a full pass over about 1,350 records is roughly 55 requests. Records are matched by DOI, or by PMID when the DOI is missing or contains parentheses; a count is saved only when the returned record has exactly that DOI or PMID.

**Set up.**

1. In Supabase, run the updated `supabase/schema.sql` again. It is safe to re-run and adds the `scopus_citations`, `scopus_citations_checked_at` and `scopus_url` columns. Until you do, the website and scripts keep working without Scopus.
2. Create an API key at [dev.elsevier.com](https://dev.elsevier.com) (My API Key). Scopus data depends on your institution's subscription: the key usually works only from the institution's network or VPN, unless Elsevier also issues you an institutional token (your library can request one).
3. Try it on your own computer, on campus or VPN:

```bash
export SUPABASE_URL='https://PROJECT.supabase.co'
export SUPABASE_SECRET_KEY='SECRET_KEY'
export SCOPUS_API_KEY='ELSEVIER_KEY'
# export SCOPUS_INSTTOKEN='TOKEN'   # only if Elsevier gave you one
python3 scripts/sync_database.py --scopus-only --scopus-max-lookups 25
```

4. For the weekly run, add repository secrets `SCOPUS_API_KEY` (and `SCOPUS_INSTTOKEN` if you have one). Without `SCOPUS_API_KEY` the weekly run skips Scopus. GitHub's servers are outside your institution's network, so a key without an institutional token may be refused there (HTTP 401/403); the log then says so and the rest of the update is unaffected.

`SCOPUS_MAX_LOOKUPS` (repository variable, default 2000) limits records per run; the API quota is 20,000 requests a week. Scopus and Google Scholar counts normally differ because they index different sources. Citation counts are never overwritten by a failed lookup, and `--no-citations` skips both Scholar and Scopus.

## Editing publications

Visitors have read-only access. An approved editor selects **Editor sign in**, enters their email and password, and then uses **Add publication** or **Edit record & associations**. Clicking **Save record** writes the record directly to Supabase; no CSV download or GitHub commit is required.

For a new PubMed publication, enter its PMID and select **Populate from PubMed** before saving. Enter one project or programme per line. These assignments are stored with a manual source flag and retained by future weekly updates.

## Backups and audit history

- **Download CSV backup** exports the currently loaded database from the browser.
- The weekly workflow commits an updated `publications.csv` to GitHub.
- `public.publication_history` stores each insert, update, and delete with its timestamp, editor ID, and before/after records.

## Local testing

Until `config.js` contains working Supabase values, the page automatically falls back to the committed CSV in read-only mode. Serve the repository with a local web server rather than opening `index.html` using a `file:` URL.

Run the citation tests with `python -m unittest discover -s scripts`. They use saved sample responses and make no network requests.
