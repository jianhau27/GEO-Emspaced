#!/usr/bin/env python3
"""
GEO optimiser for the Emspaced static mirror (Phase 1 + Phase 2).

Phase 1 - technical foundation
  * Canonical + og:url normalised to one host (fixes broken "index.html" canonicals)
  * All page URLs inside JSON-LD rewritten to the same host (no more mixed hosts)
  * Referrer-Policy + upgrade-insecure-requests meta (header-level fixes: see deploy/)
  * Heading hierarchy normalised (no skipped levels, single H1) without visual change
  * dateModified >= datePublished, future dates reported

Phase 2 - structured data + AI crawler guidance
  * Organization entity enriched (sameAs, contactPoint, knowsAbout, areaServed ...)
  * Article author upgraded from "Emspaced Admin" to a real editorial entity
  * llms.txt regenerated, llms-full.txt / ai.txt / robots.txt / sitemaps re-hosted
  * sitemap.xml regenerated with <lastmod>, pagination excluded

Usage
  python tools/geo_optimize.py                      # use site_url from geo_config.json
  python tools/geo_optimize.py --site-url https://emspaced.com
  python tools/geo_optimize.py --check              # report only, write nothing

The script is idempotent: running it again produces no further changes.
"""
import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "tools", "geo_config.json")
SKIP_DIRS = {"wp-content", "wp-includes", "wp-json", ".git", "cdn-cgi", "tools", "deploy"}
ASSET_MARKERS = ("/wp-content/", "/wp-includes/", "/wp-json/", "/wp-admin/")
PAGINATION_RE = re.compile(r"^/(blog/\d+|events/page/\d+)/$")
CHILD_CSS = os.path.join(ROOT, "wp-content", "themes", "hello-elementor-child", "style.css")
KIT_CSS = os.path.join(ROOT, "wp-content", "uploads", "elementor", "css", "post-417.css")
THEME_SIZES = {1: "2.5rem", 2: "2rem", 3: "1.75rem", 4: "1.5rem", 5: "1.25rem", 6: "1rem"}
CSS_START, CSS_END = "/* GEO-HEADINGS:START */", "/* GEO-HEADINGS:END */"

ASCII_MAP = {"\u2014": " - ", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"',
             "\u201d": '"', "\u2026": "...", "\u00a0": " ", "\u2022": "-"}


def ascii_text(s):
    for k, v in ASCII_MAP.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip()


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s, check):
    if check:
        return
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


# --------------------------------------------------------------------------- pages
def iter_pages():
    for d, dirs, files in os.walk(ROOT):
        rel = os.path.relpath(d, ROOT)
        top = rel.split(os.sep)[0]
        if top in SKIP_DIRS:
            dirs[:] = []
            continue
        if "index.html" in files:
            path = "/" if rel == "." else "/" + rel.replace(os.sep, "/") + "/"
            yield os.path.join(d, "index.html"), path


# --------------------------------------------------------------------------- hosts
class Hosts:
    def __init__(self, cfg, site):
        self.site = site.rstrip("/")
        self.known = [h.rstrip("/") for h in cfg["known_hosts"]]
        self.asset_host = cfg["production_asset_host"].rstrip("/")

    def rewrite(self, url):
        """Rewrite a page URL on any known host to the target site. Assets stay on production."""
        for h in self.known:
            if url == h or url.startswith(h + "/") or url.startswith(h + "#") or url.startswith(h + "?"):
                rest = url[len(h):]
                if any(m in rest for m in ASSET_MARKERS):
                    # images etc. only exist on production (or wherever they are mirrored)
                    return url if h in (self.asset_host, "https://www.emspaced.com") else self.asset_host + rest
                return self.site + rest
        return url

    def rewrite_text(self, text):
        """Rewrite host names in plain text files (llms-full.txt, ai.txt, robots.txt)."""
        pattern = "|".join(re.escape(h) for h in sorted(self.known, key=len, reverse=True))
        return re.sub(r"(%s)(?=[/\s\"'<)]|$)" % pattern, self.site, text)


# --------------------------------------------------------------------------- headings
HEAD_TAG = re.compile(r"<(/?)h([1-6])((?:\s[^>]*)?)>", re.I)
SCRIPT_STYLE = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>|<template\b.*?</template>)", re.S | re.I)


def normalise_headings(doc, stats):
    parts = SCRIPT_STYLE.split(doc)
    stack = []      # outline stack: (orig_level, new_level)
    open_tags = []  # currently open heading tags: new_level
    seen_h1 = [False]

    def handle(m):
        closing, level, attrs = m.group(1), int(m.group(2)), m.group(3) or ""
        if closing:
            new = open_tags.pop() if open_tags else level
            return "</h%d>" % new
        lvl = level
        if lvl == 1:
            if not seen_h1[0]:
                seen_h1[0] = True
                stack[:] = [(1, 1)]
                open_tags.append(1)
                return m.group(0)
            lvl = 2  # extra H1s become H2
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        new = min(lvl, stack[-1][1] + 1) if stack else lvl
        stack.append((lvl, new))
        open_tags.append(new)
        if new == level:
            return m.group(0)
        stats["headings_fixed"] += 1
        if re.search(r"\bgeo-h[1-6]\b", attrs):
            new_attrs = attrs
        elif re.search(r'\sclass="', attrs):
            new_attrs = re.sub(r'(\sclass=")([^"]*)"', lambda c: '%s%s geo-h%d"' % (c.group(1), c.group(2), level), attrs, count=1)
        else:
            new_attrs = ' class="geo-h%d"%s' % (level, attrs)
        return "<h%d%s>" % (new, new_attrs)

    out = []
    for i, part in enumerate(parts):
        out.append(part if i % 2 else HEAD_TAG.sub(handle, part))
    return "".join(out)


def count_skips(doc):
    body = SCRIPT_STYLE.sub("", doc)
    levels = [int(m.group(2)) for m in HEAD_TAG.finditer(body) if not m.group(1)]
    skips = sum(1 for a, b in zip(levels, levels[1:]) if b > a + 1)
    return skips, levels.count(1)


def build_heading_css():
    """CSS that keeps re-tagged headings looking exactly like their original level."""
    lines = [CSS_START,
             "/* Generated by tools/geo_optimize.py. Headings re-tagged for a valid outline keep",
             "   their original look via the geo-hN class (N = original level). Do not edit by hand. */"]
    for lvl, size in THEME_SIZES.items():
        others = ",".join("h%d" % i for i in range(1, 7) if i != lvl)
        lines.append(":is(%s):where(.geo-h%d){font-size:%s}" % (others, lvl, size))
    # Elementor kit styles h4 globally; replicate on headings that used to be h4 ...
    if os.path.exists(KIT_CSS):
        kit = read(KIT_CSS)
        others = ":is(h1,h2,h3,h5,h6):where(.geo-h4)"
        for media, body in re.findall(r"(@media[^{]*\{)?\s*\.elementor-kit-417 h4\{([^}]*)\}", kit):
            rule = ".elementor-kit-417 %s{%s}" % (others, body)
            lines.append("%s%s}" % (media, rule) if media else rule)
    # ... and neutralise the kit h4 style on headings promoted *to* h4 from another level
    for lvl in (1, 2, 3, 5, 6):
        lines.append(".elementor-kit-417 h4:where(.geo-h%d){color:inherit;font-family:inherit;font-weight:500;"
                     "font-size:%s;line-height:1.2;letter-spacing:normal}" % (lvl, THEME_SIZES[lvl]))
    lines.append(CSS_END)
    return "\n".join(lines)


# --------------------------------------------------------------------------- JSON-LD
LD_RE = re.compile(r'(<script[^>]*type="application/ld\+json"[^>]*>)(.*?)(</script>)', re.S)


def walk_strings(obj, fn):
    if isinstance(obj, dict):
        return {k: walk_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk_strings(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def types_of(node):
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else list(t)


def fix_dates(node, stats, now):
    pub, mod = node.get("datePublished"), node.get("dateModified")
    if pub and mod and mod < pub:
        node["dateModified"] = pub
        stats["dates_fixed"] += 1
    for key in ("datePublished", "dateModified"):
        v = node.get(key)
        if v and v[:10] > now:
            stats["future_dates"].append("%s %s=%s" % (node.get("@id", "?"), key, v))


def author_node(cfg, site, org_id):
    a = cfg["author"]
    node = {"@type": a["type"], "@id": site + "/#author-" + re.sub(r"[^a-z0-9]+", "-", a["name"].lower()).strip("-"),
            "name": a["name"], "url": site + a.get("url_path", "/about-us/")}
    if a["type"] == "Person":
        node["worksFor"] = {"@id": org_id}
        if a.get("job_title"):
            node["jobTitle"] = a["job_title"]
    else:
        node["parentOrganization"] = {"@id": org_id}
    if a.get("same_as"):
        node["sameAs"] = a["same_as"]
    return node


def enrich_org(node, cfg, site):
    b = cfg["brand"]
    same = list(node.get("sameAs", []))
    for s in b["same_as"]:
        if s not in same:
            same.append(s)
    node["sameAs"] = same
    node.setdefault("description", b["description"])
    node["contactPoint"] = [
        {"@type": "ContactPoint", "telephone": b["telephone"], "contactType": "customer service", "areaServed": "MY"},
        {"@type": "ContactPoint", "telephone": b["mobile"], "contactType": "customer service", "areaServed": "MY"},
    ]
    node["knowsAbout"] = b["knows_about"]
    node["areaServed"] = [{"@type": "City", "name": n} if n == "Kuala Lumpur" else
                          {"@type": "Place", "name": n} if n == "Klang Valley" else
                          {"@type": "Country", "name": n} for n in b["area_served"]]
    node["location"] = [{"@id": site + "/location/kl-sentral/#coworking-space"},
                        {"@id": site + "/location/kl-eco-city/#coworking-space"}]
    if "telephone" not in node:
        node["telephone"] = b["telephone"]


def process_jsonld(doc, cfg, hosts, stats, now):
    site = hosts.site
    org_id = site + "/#organization"
    legacy = set(cfg["author"].get("legacy_names", []))
    meta = {}
    ogm = re.search(r'<meta content="([^"]+)" property="og:image"', doc)
    og_image = hosts.rewrite(ogm.group(1)) if ogm else None

    def fix_block(m):
        try:
            data = json.loads(m.group(2))
        except json.JSONDecodeError:
            stats["invalid_jsonld"] += 1
            return m.group(0)
        data = walk_strings(data, hosts.rewrite)
        graph = data.get("@graph") if isinstance(data, dict) else None
        nodes = graph if graph is not None else (data if isinstance(data, list) else [data])
        new_nodes, has_article = [], False
        for node in nodes:
            if not isinstance(node, dict):
                new_nodes.append(node)
                continue
            t = types_of(node)
            # drop the anonymous "Emspaced Admin" person node
            if "Person" in t and node.get("name") in legacy:
                continue
            fix_dates(node, stats, now)
            if "WebPage" in t or any(x.endswith("Page") for x in t):
                meta.setdefault("modified", node.get("dateModified"))
                meta.setdefault("published", node.get("datePublished"))
            if node.get("@id") == org_id and "Organization" in t:
                enrich_org(node, cfg, site)
            if "LocalBusiness" in t and node.get("@id") != org_id:
                node["parentOrganization"] = {"@id": org_id}
                img = node.get("image")
                if isinstance(img, str) and "/images/" in img and og_image:
                    node["image"] = og_image  # placeholder path returned 404
            if "Article" in t or "BlogPosting" in t:
                has_article = True
                node["author"] = {"@id": author_node(cfg, site, org_id)["@id"],
                                  "name": cfg["author"]["name"]}
                node["publisher"] = {"@id": org_id}
                meta["article"] = True
            new_nodes.append(node)
        if has_article:
            aid = author_node(cfg, site, org_id)
            if not any(isinstance(n, dict) and n.get("@id") == aid["@id"] for n in new_nodes):
                new_nodes.append(aid)
            else:
                new_nodes = [aid if isinstance(n, dict) and n.get("@id") == aid["@id"] else n for n in new_nodes]
        if graph is not None:
            data["@graph"] = new_nodes
        elif isinstance(data, list):
            data = new_nodes
        else:
            data = new_nodes[0] if new_nodes else data
        out = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        return m.group(1) + out + m.group(3)

    return LD_RE.sub(fix_block, doc), meta


# --------------------------------------------------------------------------- head tags
def set_head_link(doc, rel, href):
    tag = '<link href="%s" rel="%s"/>' % (href, rel)
    pat = re.compile(r'<link[^>]*\brel="%s"[^>]*/?>' % rel)
    if pat.search(doc):
        return pat.sub(tag, doc, count=1)
    return doc.replace("</title>", "</title>\n" + tag, 1)


def set_meta_property(doc, prop, content):
    tag = '<meta content="%s" property="%s"/>' % (content, prop)
    pat = re.compile(r'<meta[^>]*property="%s"[^>]*/?>' % re.escape(prop))
    return pat.sub(tag, doc, count=1) if pat.search(doc) else doc


SECURITY_META = ('<meta content="strict-origin-when-cross-origin" name="referrer"/>\n'
                 '<meta content="upgrade-insecure-requests" http-equiv="Content-Security-Policy"/>')


def add_security_meta(doc):
    if 'name="referrer"' in doc:
        return doc
    m = re.search(r"<meta[^>]*charset[^>]*>", doc, re.I)
    if m:
        return doc[:m.end()] + "\n" + SECURITY_META + doc[m.end():]
    return doc.replace("<head>", "<head>\n" + SECURITY_META, 1)


def fix_article_meta_dates(doc, stats):
    pub = re.search(r'<meta content="([^"]+)" property="article:published_time"', doc)
    mod = re.search(r'<meta content="([^"]+)" property="article:modified_time"', doc)
    if pub and mod and mod.group(1) < pub.group(1):
        doc = doc.replace(mod.group(0), '<meta content="%s" property="article:modified_time"' % pub.group(1))
        stats["dates_fixed"] += 1
    return doc


# --------------------------------------------------------------------------- page meta
def page_info(doc):
    title = re.search(r"<title>(.*?)</title>", doc, re.S)
    desc = re.search(r'<meta content="([^"]*)" name="description"', doc) or \
        re.search(r'<meta name="description" content="([^"]*)"', doc)
    clean = lambda s: ascii_text(htmllib.unescape(s)) if s else ""
    t = clean(title.group(1)) if title else ""
    t = re.sub(r"\s*[-|]\s*Emspaced\s*$", "", t)
    return t, clean(desc.group(1)) if desc else ""


# --------------------------------------------------------------------------- txt files
def build_llms(cfg, site, pages):
    b = cfg["brand"]
    by_path = {p["path"]: p for p in pages}

    def link(path, fallback_title, note=None):
        p = by_path.get(path)
        title = (p["title"] if p and p["title"] else fallback_title)
        desc = note or (p["desc"] if p else "")
        return "- [%s](%s%s)%s" % (title, site, path, (": " + desc) if desc else "")

    posts = sorted([p for p in pages if p["path"].startswith(("/blog/", "/uncategorized/"))
                    and p["article"]], key=lambda p: p["published"] or "", reverse=True)
    featured = [s for s in cfg["featured_posts"]]
    feat_paths = ["/blog/%s/" % s for s in featured if "/blog/%s/" % s in by_path]
    events = sorted([p for p in pages if p["path"].startswith("/event/")], key=lambda p: p["path"])

    L = ["# Emspaced", "", "> " + b["description"], "",
         "Emspaced operates two coworking locations in Kuala Lumpur. Pricing and availability change; "
         "always confirm current plans on the Membership page or by contacting the team.", "",
         "## Business facts",
         "- Name: Emspaced",
         "- Type: Coworking space, serviced office and business-support provider",
         "- Country: Malaysia (Kuala Lumpur, Klang Valley)",
         "- KL Sentral: Level 23A, Menara Allianz Sentral, 203 Jalan Tun Sambanthan, 50470 Kuala Lumpur",
         "- KL Eco City: Level 5, Mercu 3, Jalan Bangsar, KL Eco City, 59200 Kuala Lumpur",
         "- Phone: %s | Mobile: %s" % (b["telephone"], b["mobile"]),
         "- Website: %s/" % site,
         "- Social: " + ", ".join(b["same_as"]), "",
         "## Workspace",
         link("/membership/", "Membership", "Hot desks, dedicated desks, private offices and day passes"),
         link("/location/", "Locations", "Overview of the KL Sentral and KL Eco City centres"),
         link("/location/kl-sentral/", "KL Sentral", "Coworking at Menara Allianz Sentral, next to the KL Sentral transit hub"),
         link("/location/kl-eco-city/", "KL Eco City", "Coworking at Mercu 3, KL Eco City, near Bangsar and Mid Valley"),
         "",
         "## Business services",
         link("/services/", "Services", "Overview of business-support services"),
         link("/accounting-services/", "Accounting Services", "Bookkeeping, accounting and tax-compliance support for SMEs"),
         link("/sql-accounting-software/", "SQL Accounting Software", "SQL Accounting software solutions and support"),
         link("/hrms-services/", "HR Management System", "HRMS for employee records, leave, claims and payroll workflows"),
         link("/it-solutions/", "IT Solutions", "IT, cloud and cybersecurity support for growing businesses"),
         "",
         "## Answers and company information",
         link("/faq/", "FAQ", "Direct answers about memberships, facilities, access and bookings"),
         link("/about-us/", "About Us", "Who Emspaced is, mission and approach"),
         link("/contact-us/", "Contact Us", "Phone, email form and visiting information"),
         link("/events/", "Events", "Workshops and networking events hosted at Emspaced"),
         link("/blog/", "Blog", "Guides on workspace, HR, payroll, tax and technology for Malaysian businesses"),
         "",
         "## Key guides"]
    L += [link(p, p) for p in feat_paths]
    L += ["", "## Optional", "Lower-priority pages. Paginated archives (/blog/N/, /events/page/N/) are intentionally excluded."]
    L += [link(p["path"], p["title"]) for p in posts if p["path"] not in feat_paths]
    L += [link(p["path"], p["title"]) for p in events]
    L += [link("/privacy-policy/", "Privacy Policy", "How Emspaced handles personal data"), ""]
    missing = [s for s in featured if "/blog/%s/" % s not in by_path]
    return "\n".join(ascii_text(x) if x else "" for x in L), missing


ROBOTS_EXTRA_BOTS = ["Claude-SearchBot", "Claude-User", "Perplexity-User", "Google-CloudVertexBot",
                     "DuckAssistBot", "bingbot"]


def build_robots(text, site):
    text = re.sub(r"^Sitemap:.*$\n?", "", text, flags=re.M).rstrip() + "\n"
    for bot in ROBOTS_EXTRA_BOTS:
        if not re.search(r"^User-agent:\s*%s\s*$" % re.escape(bot), text, re.M | re.I):
            text += "\nUser-agent: %s\nAllow: /\n" % bot
    text += "\n# Sitemaps\nSitemap: %s/sitemap.xml\nSitemap: %s/sitemap_index.xml\n" % (site, site)
    return text


def build_sitemap(site, pages):
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in sorted(pages, key=lambda p: p["path"]):
        if PAGINATION_RE.match(p["path"]):
            continue
        lm = (p["modified"] or p["published"] or "")[:10]
        rows.append("  <url><loc>%s%s</loc>%s</url>" % (site, p["path"], "<lastmod>%s</lastmod>" % lm if lm else ""))
    rows.append("</urlset>\n")
    return "\n".join(rows)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-url")
    ap.add_argument("--check", action="store_true", help="report only, do not write files")
    args = ap.parse_args()
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    site = (args.site_url or cfg["site_url"]).rstrip("/")
    hosts = Hosts(cfg, site)
    now = dt.date.today().isoformat()
    stats = {"pages": 0, "changed": 0, "headings_fixed": 0, "dates_fixed": 0, "invalid_jsonld": 0,
             "future_dates": [], "skips_before": 0, "skips_after": 0, "multi_h1_after": 0}
    pages = []
    legacy = cfg["author"].get("legacy_names", [])

    for path_fs, path in iter_pages():
        doc = read(path_fs)
        orig = doc
        stats["pages"] += 1
        stats["skips_before"] += count_skips(doc)[0]

        doc, meta = process_jsonld(doc, cfg, hosts, stats, now)
        doc = set_head_link(doc, "canonical", site + path)
        doc = set_meta_property(doc, "og:url", site + path)
        doc = fix_article_meta_dates(doc, stats)
        doc = add_security_meta(doc)
        doc = normalise_headings(doc, stats)
        for name in legacy:  # visible byline + meta author
            doc = doc.replace(name, cfg["author"]["name"])

        skips, h1s = count_skips(doc)
        stats["skips_after"] += skips
        stats["multi_h1_after"] += 1 if h1s > 1 else 0
        title, desc = page_info(doc)
        pages.append({"path": path, "title": title, "desc": desc, "article": bool(meta.get("article")),
                      "published": meta.get("published"), "modified": meta.get("modified")})
        if doc != orig:
            stats["changed"] += 1
            write(path_fs, doc, args.check)

    # heading CSS in child theme
    css = read(CHILD_CSS)
    block = build_heading_css()
    if CSS_START in css:
        new_css = re.sub(re.escape(CSS_START) + r".*?" + re.escape(CSS_END), lambda _: block, css, flags=re.S)
    else:
        new_css = css.rstrip() + "\n\n" + block + "\n"
    if new_css != css:
        write(CHILD_CSS, new_css, args.check)

    # text files
    llms, missing = build_llms(cfg, site, pages)
    write(os.path.join(ROOT, "llms.txt"), llms, args.check)
    for name in ("llms-full.txt", "ai.txt"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            write(p, ascii_text_lines(hosts.rewrite_text(read(p))), args.check)
    write(os.path.join(ROOT, "robots.txt"), build_robots(read(os.path.join(ROOT, "robots.txt")), site), args.check)
    write(os.path.join(ROOT, "sitemap.xml"), build_sitemap(site, pages), args.check)
    write(os.path.join(ROOT, "sitemap_index.xml"),
          '<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
          '  <sitemap>\n    <loc>%s/sitemap.xml</loc>\n    <lastmod>%s</lastmod>\n  </sitemap>\n</sitemapindex>\n'
          % (site, now), args.check)

    print("GEO optimiser  site=%s  mode=%s" % (site, "CHECK" if args.check else "WRITE"))
    for k in ("pages", "changed", "headings_fixed", "dates_fixed", "invalid_jsonld",
              "skips_before", "skips_after", "multi_h1_after"):
        print("  %-16s %s" % (k, stats[k]))
    print("  future_dates     %d" % len(stats["future_dates"]))
    for f in stats["future_dates"][:10]:
        print("     ", f)
    if missing:
        print("  WARNING featured posts not found:", missing)
    return 0


def ascii_text_lines(text):
    return "\n".join(ascii_text_keep_space(line) for line in text.splitlines()) + "\n"


def ascii_text_keep_space(line):
    for k, v in ASCII_MAP.items():
        line = line.replace(k, v)
    return line.rstrip()


if __name__ == "__main__":
    sys.exit(main())
