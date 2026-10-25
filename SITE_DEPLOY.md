# DLNE public site

Three static files — `index.html`, `favicon.ico`, `favicon.png`. No build step, no
dependencies, nothing to install. Every style rule and the hero canvas are inline in
`index.html`, so the page works opened straight from disk and works identically on any
host that can serve a file.

Preview it before deploying:

    python -m http.server 8000 --directory site

then open <http://localhost:8000>.

## Publishing it free

Any of these are genuinely free at this scale. Cloudflare Pages is the recommendation:
no repository required, no cold starts, no bandwidth ceiling worth worrying about, and
a custom domain costs nothing on top.

### Cloudflare Pages — drag and drop

1. <https://dash.cloudflare.com> → **Workers & Pages** → **Create** → **Pages** →
   **Upload assets**.
2. Drag the `site` folder in. Name the project `dlne`.
3. Live at `https://dlne.pages.dev` within a minute.
4. Custom domain, if you have one: **Custom domains** → **Set up a domain**. Cloudflare
   issues the TLS certificate itself.

To update later, upload the folder again — it redeploys in place and keeps the URL.

### GitHub Pages — if you want the site in version control

**The site must live in its own PUBLIC repository.** Do not enable Pages on the DLNE
repo: it is private for good reason, and Pages on a private repo either requires a paid
plan or, worse, tempts you into making the whole thing public.

    # from a scratch directory, NOT inside the DLNE working tree
    mkdir dlne-site && cd dlne-site
    git init
    cp -r /path/to/DistributedLLMEngine/site/* .
    git add -A && git commit -m "DLNE public site"
    gh repo create dlne-site --public --source=. --push

Then **Settings → Pages → Source: deploy from branch → `main` / root**. Live at
`https://<user>.github.io/dlne-site/` in a couple of minutes.

Note the subpath: with the site in a project repo rather than `<user>.github.io`, every
URL gains `/dlne-site/`. That is fine here because every link on the page is either an
in-page anchor or an absolute Discord URL — but it is the thing that breaks first if
anyone adds a second page later.

### Netlify Drop — fastest possible

<https://app.netlify.com/drop>, drag the `site` folder, done. Gives a random subdomain
you can rename once. Worth knowing about for a throwaway preview link; Cloudflare is
the better home for something permanent.

## Before you announce it

- **`og:url` in `index.html` still says `REPLACE-WITH-YOUR-DOMAIN`.** Set it to the real
  address after the first deploy. Discord reads it when it builds the link preview.
- **No `og:image` yet.** Discord will render a text-only card. Drop a 1200×630 PNG next
  to `index.html` and add `<meta property="og:image" content="https://your-domain/og.png">`
  — the URL must be absolute or the preview silently ignores it.
- **The Discord invite is hardcoded in five places.** Search for `discord.gg/` before
  publishing if that link is ever regenerated.
- **There are no download links on the page.** Every route to the software goes through
  Discord, which is right while the alpha is invite-gated. Revisit the "How to join"
  section the moment `releases.json` is live and downloads become self-serve.
- Test the link preview with <https://www.opengraph.xyz> once it is deployed. Discord
  caches previews aggressively and getting it wrong first is annoying to undo.

## Keeping it in step with the app

The palette is copied from `webui/style.css` — the same seven custom properties, same
values. If the WebUI's accent or panel colours ever change, change them here too or the
site and the client start looking like two different products.

The page also states version `1.0.2` in the hero chip, the footer and `og.png`, and describes the
`.dlnejoin` flow from `engine/setup_core.py`. Both are claims about the software that
can go stale.
