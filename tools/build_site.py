#!/usr/bin/env python3
"""Build the quiz site.

    dist/        public site: pages, icons, data/index.json (topic list), data/author.json,
                 robots.txt + sitemap.xml, and home/ (the personal homepage served at the bare
                 domain, built from content/author.json)
    dist-data/   question banks, one <id>.json per topic. Deployed to a private bucket and only
                 served by the API to signed-in users, never as public files.

Usage:
    python tools/build_site.py                 # build both
    python tools/build_site.py --serve         # build, then preview on http://localhost:8000
                                               # (no sign-in locally; /api is emulated)
"""
import argparse
import datetime
import functools
import html
import http.server
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "pipeline"))
from validate import validate_file  # noqa: E402
from envfile import load_env  # noqa: E402  SITE_DOMAIN / HOSTED_ZONE for absolute URLs

SITE = ROOT / "site"
QUESTIONS = ROOT / "content" / "questions"
DIST = ROOT / "dist"
DIST_DATA = ROOT / "dist-data"
PUBLIC_FIELDS = ("id", "type", "style", "difficulty", "prompt", "options", "answer",
                 "explanation", "tags", "slideRef", "docUrl")


def build() -> int:
    files = sorted(QUESTIONS.glob("*.json"))
    problems = {f.name: errs for f in files if (errs := validate_file(f, approved_only=True))}
    if problems:
        for name, errs in problems.items():
            print(f"FAIL {name}")
            for e in errs:
                print(f"   - {e}")
        print("\nnot building: fix the files above (python tools/validate.py --approved-only)")
        return 1

    for d in (DIST, DIST_DATA):
        if d.exists():
            shutil.rmtree(d)
    shutil.copytree(SITE, DIST)
    (DIST / "data").mkdir()
    DIST_DATA.mkdir()

    services = []
    for f in files:
        bank = json.loads(f.read_text(encoding="utf-8"))
        decks = {q.get("sourceDeck") for q in bank["questions"]}
        fields = PUBLIC_FIELDS if len(decks) <= 1 else tuple(f for f in PUBLIC_FIELDS if f != "slideRef")
        qs = [{k: q[k] for k in fields if k in q} for q in bank["questions"]]
        svc = bank["service"]
        out = {"service": svc, "questions": qs}
        (DIST_DATA / f.name).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                                             encoding="utf-8")
        entry = {**svc, "count": len(qs)}
        icon = ROOT / "content" / "icons" / f"{svc['id']}.svg"
        if icon.exists():
            (DIST / "icons").mkdir(exist_ok=True)
            shutil.copyfile(icon, DIST / "icons" / icon.name)
            entry["icon"] = f"icons/{icon.name}"
        services.append(entry)
        print(f"  {svc['id']:<16} {len(qs):>3} questions")

    author = ROOT / "content" / "author.json"
    if author.exists():
        json.loads(author.read_text(encoding="utf-8"))  # fail early on a typo
        shutil.copyfile(author, DIST / "data" / "author.json")
        print("  author.json copied")

    search_files(services)
    services.sort(key=lambda s: (s["category"], s["name"]))
    (DIST / "data" / "index.json").write_text(json.dumps({"services": services}, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    total = sum(s["count"] for s in services)
    print(f"built dist/ and dist-data/ with {len(services)} service(s), {total} questions")
    return 0


def search_files(services) -> None:
    """Things search engines read: the bare-domain homepage, canonical URLs, robots.txt, sitemap.xml.

    With SITE_DOMAIN and HOSTED_ZONE set (.env), the quiz lives at https://SITE_DOMAIN/ and the
    homepage at https://HOSTED_ZONE/ (CloudFront serves /home/ there). Without them, URLs are relative.
    """
    load_env()
    site = os.environ.get("SITE_DOMAIN", "").strip()
    zone = os.environ.get("HOSTED_ZONE", "").strip()
    quiz_url = f"https://{site}/" if site else "/"
    home_url = f"https://{zone}/" if zone and zone != site else (quiz_url + "home/" if site else "/home/")
    author_file = ROOT / "content" / "author.json"
    a = json.loads(author_file.read_text(encoding="utf-8")) if author_file.exists() else None
    e = html.escape
    total = sum(s["count"] for s in services)
    year = datetime.date.today().year

    # quiz page: canonical address + structured data naming the site and its author
    index = DIST / "index.html"
    page = index.read_text(encoding="utf-8")
    head = []
    if site:
        head += [f'<link rel="canonical" href="{quiz_url}">', f'<meta property="og:url" content="{quiz_url}">']
    website = {"@context": "https://schema.org", "@type": "WebSite", "name": "Know Your Cloud", "url": quiz_url,
               "description": "Short, practical quizzes on the AWS services engineers use every day."}
    if a:
        website["author"] = {"@type": "Person", "name": a["name"], "url": home_url}
    head.append(f'<script type="application/ld+json">{json.dumps(website, ensure_ascii=False)}</script>')
    index.write_text(page.replace("</head>", "  " + "\n  ".join(head) + "\n</head>", 1), encoding="utf-8")

    # homepage
    home_dir = DIST / "home"
    if a and (home_dir / "index.html").exists():
        role = a.get("title", "")
        short = role.split(" at ")[0] or "Engineer"
        role_line = " · ".join(x for x in (role, a.get("location")) if x)
        bio = a.get("homeBio") or a.get("bio") or [f"{a['name']} is a {role.lower()}." if role else a["name"]]
        links = a.get("links", {})
        person = {"@type": "Person", "name": a["name"], "url": home_url, "jobTitle": short,
                  "sameAs": list(links.values()) + [quiz_url]}
        if a.get("location"):
            city, _, region = a["location"].partition(", ")
            person["address"] = {"@type": "PostalAddress", "addressLocality": city, "addressRegion": region}
        ld = {"@context": "https://schema.org", "@type": "ProfilePage", "url": home_url, "mainEntity": person}
        fill = {
            "NAME": e(a["name"]), "SHORT_TITLE": e(short), "ROLE_LINE": e(role_line), "HOME_URL": e(home_url),
            "QUIZ_URL": e(quiz_url), "YEAR": str(year),
            "DESCRIPTION": e(f"{a['name']}, {role.lower() if role else 'engineer'}"
                             f"{' in ' + a['location'] if a.get('location') else ''}. Creator of Know Your Cloud, "
                             "practical AWS quizzes for engineers."),
            "INITIALS": e("".join(w[0] for w in a["name"].split())[:2].upper()),
            "BIO": "\n".join(f"      <p>{e(p)}</p>" for p in bio),
            "QUIZ_BLURB": e(f"{total} practical questions on {len(services)} AWS topic{'s' if len(services) != 1 else ''}, "
                            "each with an explanation."),
            "LINKS": "\n".join(f'        <li><a href="{e(u)}" rel="me noopener">{e(k)}</a></li>'
                               for k, u in links.items()),
            "JSON_LD": json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"),
        }
        tpl = (home_dir / "index.html").read_text(encoding="utf-8")
        for k, v in fill.items():
            tpl = tpl.replace("{{" + k + "}}", v)
        (home_dir / "index.html").write_text(tpl, encoding="utf-8")
    elif home_dir.exists():
        shutil.rmtree(home_dir)

    # robots.txt + sitemap.xml: one pair for the quiz host, one for the bare domain (home/)
    today = datetime.date.today().isoformat()
    def pair(folder: Path, url: str) -> None:
        folder.mkdir(exist_ok=True)
        sm = url + "sitemap.xml" if url.startswith("https://") else None
        (folder / "robots.txt").write_text("User-agent: *\nDisallow: /api/\n" + (f"Sitemap: {sm}\n" if sm else ""),
                                           encoding="utf-8")
        (folder / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{e(url)}</loc><lastmod>{today}</lastmod></url>\n</urlset>\n", encoding="utf-8")
    pair(DIST, quiz_url)
    if home_dir.exists() and zone and zone != site:
        pair(home_dir, home_url)
    print(f"  search files: quiz {quiz_url}, home {home_url}")


class DevHandler(http.server.SimpleHTTPRequestHandler):
    """Static files plus a stand-in for the deployed API (same shapes). Sign-in is off locally;
    you play as "You (local)" and the leaderboard lives in memory until you stop the server."""
    progress = {"best": {}, "missed": {}}
    answered = set()        # question ids answered once (first answer decides the point)
    points = 0
    BOTS = [("Avery K.", 42), ("Sam R.", 17), ("Priya N.", 9)]  # so the board isn't empty

    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    @staticmethod
    def _bank(topic: str):
        f = DIST_DATA / f"{topic}.json"
        return json.loads(f.read_text(encoding="utf-8")) if re.fullmatch(r"[a-z0-9-]+", topic) and f.exists() else None

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/config.json":
            return self._json(200, {"auth": None})
        if path == "/api/progress":
            return self._json(200, DevHandler.progress)
        if path == "/api/profile":
            return self._json(200, {"name": "You (local)", "needsName": False})
        if path == "/api/leaderboard":
            rows = sorted(DevHandler.BOTS + ([("You (local)", DevHandler.points)] if DevHandler.points else []),
                          key=lambda r: -r[1])
            top, rank, prev = [], 0, None
            for i, (name, pts) in enumerate(rows):
                if pts != prev:
                    rank, prev = i + 1, pts
                top.append({"rank": rank, "name": name, "points": pts, "me": name == "You (local)"})
            me = next(({"rank": r["rank"], "points": r["points"], "name": r["name"]} for r in top if r["me"]), None)
            return self._json(200, {"board": "all" if "board=all" in query else "week", "top": top, "me": me})
        m = re.fullmatch(r"/api/questions/([a-z0-9-]+)", path)
        if m:
            bank = self._bank(m.group(1))
            if not bank:
                return self._json(404, {"message": "unknown topic"})
            keep = ("id", "type", "style", "difficulty", "prompt", "options", "tags", "slideRef", "docUrl")
            return self._json(200, {"service": bank["service"], "questions": [
                {**{k: q[k] for k in keep if k in q}, "need": len(q["answer"])} for q in bank["questions"]]})
        if path.startswith("/api/"):
            return self._json(404, {"message": "not found"})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/api/answer":
            return self._json(404, {"message": "not found"})
        body = self._body()
        qid, picked = str(body.get("qid", "")), body.get("picked") or []
        bank = self._bank(qid.rsplit("-", 1)[0])
        q = next((x for x in (bank or {}).get("questions", []) if x["id"] == qid), None)
        if not q:
            return self._json(404, {"message": "unknown question"})
        correct = set(picked) == set(q["answer"])
        first = qid not in DevHandler.answered
        DevHandler.answered.add(qid)
        if correct and first:
            DevHandler.points += 1
        return self._json(200, {"correct": correct, "answer": q["answer"], "explanation": q.get("explanation", ""),
                                "docUrl": q.get("docUrl"), "slideRef": q.get("slideRef"),
                                "point": correct and first, "firstTry": first})

    def do_PUT(self):  # noqa: N802
        if self.path.split("?")[0] != "/api/progress":
            return self._json(404, {"message": "not found"})
        DevHandler.progress = self._body()
        return self._json(200, DevHandler.progress)


def serve(port: int) -> None:
    handler = functools.partial(DevHandler, directory=str(DIST))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving http://localhost:{port}  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    rc = build()
    if rc == 0 and args.serve:
        serve(args.port)
    return rc


if __name__ == "__main__":
    sys.exit(main())
