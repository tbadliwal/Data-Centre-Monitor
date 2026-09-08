# Davis Data Center Regulatory Monitor — production deployment

This package fixes the live-feed problem by **removing external API calls from the browser**.

## Architecture
- `index.html` reads `data/live.json` from the same website origin.
- `scripts/update_live_data.py` runs on the server/CI runner and queries the public GDELT DOC 2.0 API.
- `.github/workflows/refresh-and-deploy.yml` refreshes the data every 6 hours and redeploys the site.
- If an upstream request fails, the updater preserves the last successful state feed instead of replacing it with junk or a blank result.
- Live candidates remain separate from the curated/verified regulatory record.

## Important security note
GitHub Pages is generally public. Do **not** publish Davis-internal analysis there unless Davis approves public exposure. The same files can be deployed to an approved internal static host; the scheduled Python updater can run in any CI/scheduler.

## GitHub Pages prototype
1. Create a repository and upload the contents of this folder.
2. In **Settings → Pages**, set **Source = GitHub Actions**.
3. Push to `main` or run the workflow manually.
4. The workflow refreshes discovery data and deploys the site.
5. The site then has one permanent bookmarkable URL.

The scheduled refresh is every 6 hours (`17 */6 * * *`). Change the cron if needed.
