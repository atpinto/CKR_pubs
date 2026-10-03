# CKR publication register

The Centre for Kidney Research publication register uses a public Supabase PostgreSQL database, a static GitHub Pages interface, and a weekly PubMed synchronization.

```text
repository root/
├── .github/workflows/update-publications.yml
├── scripts/
│   ├── update_publications.py
│   ├── sync_database.py
│   ├── scopus_citations.py
│   ├── social_posts.py
│   ├── citations_common.py
│   └── test_*.py
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
- `SCOPUS_API_KEY` (and optionally `SCOPUS_INSTTOKEN`) as optional repository secrets, and `SCOPUS_MAX_LOOKUPS` as an optional variable (see section 5).

For the first run, select **Import publications.csv without querying PubMed**. On later manual runs, leave it unselected so PubMed is refreshed normally.

The workflow runs every Monday at 7:00 am in the `Australia/Sydney` timezone. It downloads the live database first, refreshes PubMed fields, preserves associations marked as manual, writes the result back to Supabase, and then refreshes Scopus citation counts when a key is configured. It commits `publications.csv` as a backup.

If the workflow cannot commit the CSV, open **Settings → Actions → General → Workflow permissions** and allow workflows to write repository contents.

## 5. Citation counts (Scopus)

Each publication can show a **Cited by** count from Scopus. Google Scholar counts were tried and removed: Scholar has no official API and blocks automated requests.

Counts are shown as "Cited by N in Scopus", linking to Scopus. They come from Elsevier's official Scopus Search API, so there is no CAPTCHA, and one request covers 12 records: a full pass over about 1,350 records is roughly 115 requests. Records are matched by DOI, or by PMID when the DOI is missing or contains parentheses; a count is saved only when the returned record has exactly that DOI or PMID.

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

`SCOPUS_MAX_LOOKUPS` (repository variable, default 2000) limits records per run; the API quota is 20,000 requests a week. Citation counts are never overwritten by a failed lookup, and `--no-citations` skips the Scopus refresh.

## 6. LinkedIn posts for new publications

After each weekly update, `scripts/social_posts.py` writes a LinkedIn post for every publication added since the previous run and schedules it on the CKR LinkedIn page through [Buffer](https://buffer.com). Posts go out on weekdays at 9am, 12pm and 3pm Sydney time, three hours apart. A larger batch carries over to the following days, and a new batch always starts at least three hours after the last post already queued. Public holidays are not skipped.

Each post has a two- or three-sentence plain-language summary that Claude (`claude-opus-5-5`) writes from the PubMed abstract. Below the summary come the title, the first author and journal, a DOI link (or a PubMed link if there is no DOI), and the hashtags. Posts are scheduled without review. Edit or delete them in Buffer's queue before they go out if needed.

- Only publications added to the register on or after `SOCIAL_POSTS_SINCE` are posted, so the existing collection is never announced.
- Papers from before last calendar year are skipped. PubMed sometimes adds older papers late.
- At most 10 posts are scheduled per run. Buffer's free plan holds 10 scheduled posts per channel. If the queue is full, the remaining papers wait for the next run.
- The `social_posts` table records every publication already handled, so nothing is posted twice. It is only readable with the secret key. Its `status` column holds one of these values:
  - `scheduled`
  - `skipped`
  - `failed`: Buffer refused the post. The reason is in `note`.
  - `scheduling`: the run stopped part-way. Check Buffer by hand.

  To retry a publication, delete its row.

**Set up.**

1. Run the updated `supabase/schema.sql` in the Supabase SQL editor. It is safe to re-run, and it adds the `social_posts` table.
2. In Buffer, connect the CKR LinkedIn **page** as a channel. Then create an API key in [Settings → API](https://publish.buffer.com/settings/api).
3. Create an Anthropic API key at [platform.claude.com](https://platform.claude.com). Each post costs well under one cent.
4. Find the Buffer IDs on your own computer:

```bash
export BUFFER_API_KEY='BUFFER_KEY'
python3 scripts/social_posts.py --list-channels
```

5. In **GitHub → Settings → Secrets and variables → Actions**, add:
   - repository secrets: `BUFFER_API_KEY` and `ANTHROPIC_API_KEY`;
   - repository variables: `BUFFER_ORGANIZATION_ID` and `BUFFER_CHANNEL_ID` (the LinkedIn page's channel);
   - repository variable `SOCIAL_POSTS_SINCE`: the date posting starts, e.g. `2026-10-05`.
6. Test it: run the workflow by hand with **Write the LinkedIn posts … but schedule nothing** ticked, and read the posts in the log.

Until all three of `BUFFER_API_KEY`, `ANTHROPIC_API_KEY` and `SOCIAL_POSTS_SINCE` are set, the step does nothing.

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
