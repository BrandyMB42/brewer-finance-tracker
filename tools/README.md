# Local setup tools

Utilities that are run **on your own machine** to set up the Brewer Finance
Tracker. Nothing in this directory is deployed — these are one-time / occasional
helpers. The whole `tools/` directory is git-ignored (see the repo
`.gitignore`); the source is committed once so the team has it, but anything you
drop in here locally (especially `.env`) stays off git.

## `plaid-link-setup/` — one-time bank account connection

A tiny local web tool that walks Plaid Link once **per card/Item** and stores the
resulting long-lived **access token** in GCP Secret Manager as
`plaid-access-token-{institution-slug}-{disambiguator}`, where the disambiguator
is the account's last-4 (mask) or an item_id suffix. Including a disambiguator
keeps each Item's token in its own secret, so connecting a **second card at the
same institution** does not overwrite the first. The deployed tracker reads all
`plaid-access-token-*` secrets at runtime — so after setup, you don't need this
tool for day-to-day use.

```
plaid-link-setup/
├── index.html        # local UI — opens Plaid Link, shows connected institutions
├── server.py         # local Flask API — talks to Plaid + Secret Manager
├── requirements.txt  # flask, plaid-python, python-dotenv, requests
└── .env.example      # template for your local Plaid credentials
```

### Why it reads credentials from `.env` (not Secret Manager)

The deployed app pulls Plaid credentials *from* Secret Manager. This tool is the
bootstrap that runs *before* that flow is wired up, so it reads
`PLAID_CLIENT_ID` / `PLAID_SECRET` from a local `.env`. It still **writes** the
access tokens it obtains *into* Secret Manager via the `gcloud` CLI.

### Test in sandbox first, then production

Run it against the Plaid **sandbox** first to confirm the whole flow works
end-to-end (sandbox lets you log in with Plaid's test credentials, e.g.
`user_good` / `pass_good`). Once you've verified it, swap your `.env` to your
**production** Plaid secret and `PLAID_ENV=production`, then connect the real
accounts.

> ⚠️ Sandbox and production access tokens are not interchangeable. Tokens created
> in sandbox only work against sandbox. Connect your real accounts in production
> mode.

### Step by step

1. **Authenticate gcloud** (the tool stores secrets via the CLI):
   ```bash
   gcloud auth login
   gcloud config set project <your-gcp-project-id>
   ```
2. **Configure credentials**:
   ```bash
   cd tools/plaid-link-setup
   cp .env.example .env
   # edit .env — start with PLAID_ENV=sandbox and your sandbox secret
   ```
3. **Install dependencies** (a virtualenv is recommended):
   ```bash
   pip install -r requirements.txt
   ```
4. **Start the local server**:
   ```bash
   python server.py
   # serves http://localhost:5000
   ```
5. **Open the UI**: open `index.html` in your browser (double-click it, or use
   `file://` — the server allows that origin). Click **Connect a bank account**
   and complete Plaid Link. On success you'll see the institution name, its
   `item_id`, and the Secret Manager secret it was stored under.
6. **Repeat** for each institution. The "Already connected" list reflects the
   `plaid-access-*` secrets that exist.
7. **Go to production**: stop the server, set `PLAID_ENV=production` and your
   production secret in `.env`, restart, and connect the real accounts.

### After setup

Once every account is connected, the access tokens live in Secret Manager and
the deployed tracker uses them automatically. You can stop the server and ignore
this tool until you need to add or re-link an institution.

### Endpoints (for reference)

| Method | Path                 | Purpose |
|--------|----------------------|---------|
| `POST` | `/create-link-token` | Create a short-lived Link token for the browser. |
| `POST` | `/exchange-token`    | Exchange the public token; store access token as `plaid-access-token-{slug}-{disambiguator}` (last-4 or item_id suffix). |
| `GET`  | `/status`            | List already-connected institutions + active Plaid env. |
