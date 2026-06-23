# iCloud Hide My Email Mail Viewer

This Docker service searches one or more authorized receiving mailboxes for mail forwarded from iCloud Hide My Email aliases.

It does not turn iCloud aliases into independent mailboxes. Each alias still forwards to your real receiving mailbox. This service simply gives you your own private lookup page, so exported links do not need to open Gmail in the browser.

## Deploy

1. Copy the config file:

```bash
cp .env.example .env
```

2. Edit `.env`:

```bash
PUBLIC_BASE_URL=http://YOUR_SERVER_IP:8787
VIEWER_TOKEN=use-a-long-random-viewer-secret
ADMIN_TOKEN=use-a-long-random-admin-secret
```

3. Start the container:

```bash
docker compose up -d --build
```

4. Open the admin page:

```text
http://YOUR_SERVER_IP:8787/admin?key=use-a-long-random-admin-secret
```

5. Add one or more main mailboxes from the page.

For Gmail, enable IMAP and create an App Password. Do not use your normal Gmail password.

## Tampermonkey Template

After the service is running, set `查看链接模板` in the Tampermonkey helper panel to:

```text
http://YOUR_SERVER_IP:8787/show/{encodedEmail}?key=use-a-long-random-viewer-secret
```

Then export again. Each line will look like:

```text
alias@icloud.com----http://YOUR_SERVER_IP:8787/show/alias%40icloud.com?key=use-a-long-random-viewer-secret
```

## Multi-Mailbox Behavior

- The admin page stores accounts in `/data/config.json`.
- `docker-compose.yml` mounts a named volume, so mailbox settings survive container rebuilds.
- Searches run across every enabled account.
- Each message result shows which main mailbox matched.
- You can add, delete, enable, disable, and test mailbox connections from the page.

## Useful Settings

- `VIEWER_TOKEN`: required for exported `/show/...` links.
- `ADMIN_TOKEN`: recommended for the mailbox configuration page. If omitted, `VIEWER_TOKEN` is also used for admin.
- `CONFIG_PATH`: defaults to `/data/config.json`.
- `MAX_RESULTS`: total results returned per lookup across all enabled accounts.
- `RECENT_DAYS`: default value shown when adding an account.
- `STRICT_LOCAL_FILTER`: keep this as `0` for Gmail. Gmail can match `deliveredto:` internally even when the alias is not visible in downloaded message headers.

## Reverse Proxy

If you expose this on the public internet, put it behind HTTPS. The exported template should then use your HTTPS domain:

```text
https://mail-viewer.example.com/show/{encodedEmail}?key=use-a-long-random-viewer-secret
```
