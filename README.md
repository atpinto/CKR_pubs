# CKR publication register

The Centre for Kidney Research publication register uses a public Supabase PostgreSQL database, a static GitHub Pages interface, and a weekly PubMed synchronization.

```text
repository root/
├── .github/workflows/update-publications.yml
├── scripts/
│   ├── update_publications.py
│   └── sync_database.py
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
- `NCBI_EMAIL` as an optional repository variable.

For the first run, select **Import publications.csv without querying PubMed**. On later manual runs, leave it unselected so PubMed is refreshed normally.

The workflow runs every Monday at 7:00 am in the `Australia/Sydney` timezone. It downloads the live database first, refreshes PubMed fields, preserves associations marked as manual, writes the result back to Supabase, and commits `publications.csv` as a backup.

If the workflow cannot commit the CSV, open **Settings → Actions → General → Workflow permissions** and allow workflows to write repository contents.

## Editing publications

Visitors have read-only access. An approved editor selects **Editor sign in**, enters their email and password, and then uses **Add publication** or **Edit record & associations**. Clicking **Save record** writes the record directly to Supabase; no CSV download or GitHub commit is required.

For a new PubMed publication, enter its PMID and select **Populate from PubMed** before saving. Enter one project or programme per line. These assignments are stored with a manual source flag and retained by future weekly updates.

## Backups and audit history

- **Download CSV backup** exports the currently loaded database from the browser.
- The weekly workflow commits an updated `publications.csv` to GitHub.
- `public.publication_history` stores each insert, update, and delete with its timestamp, editor ID, and before/after records.

## Local testing

Until `config.js` contains working Supabase values, the page automatically falls back to the committed CSV in read-only mode. Serve the repository with a local web server rather than opening `index.html` using a `file:` URL.
