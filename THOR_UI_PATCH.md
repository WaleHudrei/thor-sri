# Thor UI Patch — Scraper Picker

This patch adds a **scraper picker landing page** to the existing `thor-mycase` service. When the user clicks "Scraper" in the sidebar, they now land on `/scraper` where they choose between MyCase and SRI before seeing the form.

This is the minimum change needed to surface `thor-sri` through the existing UI. It's a **Phase 1** patch; Phase 2 will extract everything into a dedicated `thor-ui` service.

---

## 1. Add environment variable to thor-mycase service

In Railway, on `thor-mycase`:

```
THOR_SRI_URL=https://thor-sri-production.up.railway.app
```

---

## 2. Add a new route in `app.py`

Add next to your existing `/` and `/jobs` routes:

```python
import os

SCRAPER_REGISTRY = {
    "mycase": {
        "name": "MyCase",
        "description": "Indiana court filings (EV, MF, CC, SC, Probate)",
        "icon": "⚖️",
        "url": "/",  # stays on thor-mycase
    },
    "sri": {
        "name": "SRI Services",
        "description": "Tax, commissioner, and sheriff sale auctions",
        "icon": "🏛️",
        "url": os.getenv("THOR_SRI_URL", ""),
    },
}


@app.route("/scraper")
def scraper_picker():
    """Scraper picker landing page."""
    return send_from_directory("static", "scraper.html")


@app.route("/api/scrapers", methods=["GET"])
def scrapers_config():
    """Expose registry to the frontend."""
    return jsonify({"scrapers": SCRAPER_REGISTRY})
```

---

## 3. Create `static/scraper.html` (picker page)

This goes in your existing `thor-mycase/static/` directory.

```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Thor — Scrapers</title>
<style>
  :root {
    --bg-0: #0a0a0b; --bg-1: #131316; --bg-2: #1a1a1f;
    --border: #2a2a33; --text: #e8e8ee; --text-dim: #9094a3;
    --accent: #5b8cff; --success: #2dd4a7;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg-0); color: var(--text);
    font-family: 'Inter', -apple-system, sans-serif; min-height: 100vh;
  }
  .container { max-width: 960px; margin: 0 auto; padding: 60px 24px; }
  h1 { font-size: 32px; margin: 0 0 8px; font-weight: 800; }
  .subtitle { color: var(--text-dim); margin-bottom: 40px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }
  .card {
    background: var(--bg-1); border: 1px solid var(--border); border-radius: 12px;
    padding: 28px; cursor: pointer; transition: .15s; text-decoration: none; color: inherit;
    display: block; position: relative;
  }
  .card:hover { border-color: var(--accent); transform: translateY(-2px); }
  .card.disabled { opacity: 0.5; cursor: not-allowed; }
  .icon { font-size: 32px; margin-bottom: 16px; }
  .card-title { font-size: 18px; font-weight: 700; margin-bottom: 8px; }
  .card-desc { color: var(--text-dim); font-size: 14px; line-height: 1.5; }
  .status-pill {
    position: absolute; top: 16px; right: 16px;
    padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600;
  }
  .status-pill.ok { background: rgba(45,212,167,.15); color: var(--success); }
  .status-pill.down { background: rgba(244,63,94,.15); color: #f43f5e; }
  .status-pill.loading { background: var(--bg-2); color: var(--text-dim); }
</style>
</head>
<body>
<div class="container">
  <h1>Choose a scraper</h1>
  <p class="subtitle">Each scraper runs in its own Railway service with its own queue. Running one does not block others.</p>
  <div class="grid" id="scraperGrid">Loading…</div>
</div>
<script>
async function init() {
  const res = await fetch("/api/scrapers");
  const { scrapers } = await res.json();
  const grid = document.getElementById("scraperGrid");
  grid.innerHTML = "";

  for (const [key, s] of Object.entries(scrapers)) {
    const disabled = !s.url;
    const el = document.createElement("a");
    el.className = "card" + (disabled ? " disabled" : "");
    el.href = disabled ? "#" : s.url;
    el.innerHTML = `
      <div class="status-pill loading" id="pill-${key}">CHECKING</div>
      <div class="icon">${s.icon}</div>
      <div class="card-title">${s.name}</div>
      <div class="card-desc">${s.description}</div>
    `;
    grid.appendChild(el);

    // Ping health endpoint
    if (!disabled && key !== "mycase") {
      try {
        const h = await fetch(s.url + "/api/health", { mode: "cors" });
        const pill = document.getElementById(`pill-${key}`);
        if (h.ok) { pill.className = "status-pill ok"; pill.textContent = "ONLINE"; }
        else { pill.className = "status-pill down"; pill.textContent = "DOWN"; }
      } catch {
        const pill = document.getElementById(`pill-${key}`);
        pill.className = "status-pill down";
        pill.textContent = "UNREACHABLE";
      }
    } else {
      document.getElementById(`pill-${key}`).remove();
    }
  }
}
init();
</script>
</body></html>
```

---

## 4. Update sidebar in `index.html` (and `jobs.html`, `changelog.html`)

Change the "Scraper" link target from `/` to `/scraper`:

```html
<!-- before -->
<a href="/" class="sidebar-link active"><span class="icon">⬡</span> Scraper</a>

<!-- after -->
<a href="/scraper" class="sidebar-link"><span class="icon">⬡</span> Scrapers</a>
```

---

## 5. Build the SRI form UI (future — Phase 1 can skip)

For now, when the user clicks the SRI card on the picker, it takes them directly to the `thor-sri` service. That service doesn't have a UI yet — they'll see a JSON 404 from Flask at `/`.

**Two options for Phase 1:**

**Option A (do nothing):** The card goes to `https://thor-sri-production.up.railway.app/api/health`. User sees JSON health output. Ugly but functional — they know the service is alive.

**Option B (recommended):** Add a minimal `static/index.html` to `thor-sri` that's just a placeholder form saying "SRI scraper UI coming in Phase 2 — trigger via API for now: POST /api/scrape with body `{\"params\": {...}}`".

For Phase 2, thor-ui will render a proper form for each scraper based on a `param_schema.json` it fetches from each service.

---

## Summary of files changed in thor-mycase

- `app.py` — add `SCRAPER_REGISTRY`, `/scraper` route, `/api/scrapers` route
- `static/scraper.html` — new file
- `static/index.html` — sidebar link updated
- `static/jobs.html` — sidebar link updated
- `static/changelog.html` — sidebar link updated

That's it. The existing MyCase form stays at `/`, but the sidebar now takes users to the picker first.
