"""
บอทข่าวด่วนทองคำ (XAUUSD) -> แจ้งเตือนมือถือผ่าน ntfy
- ดึงข่าวจาก RSS หลายแหล่ง
- คัดเฉพาะข่าวที่เกี่ยวกับทอง / เฟด / น้ำมัน-อิหร่าน / ตัวเลขเศรษฐกิจสหรัฐ
- ข่าวเดิมส่งครั้งเดียว (จำไว้ใน seen.json)
ใช้แค่ไลบรารีมาตรฐานของ Python ไม่ต้องติดตั้งอะไรเพิ่ม
"""
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
SEEN_FILE = os.environ.get("SEEN_FILE", "seen.json")
MAX_AGE_MIN = int(os.environ.get("MAX_AGE_MIN", "45"))      # ส่งเฉพาะข่าวที่อายุไม่เกินกี่นาที
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "6"))       # กันข่าวท่วมมือถือ
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"              # 1 = พิมพ์อย่างเดียว ไม่ส่ง

UA = "Mozilla/5.0 (gold-news-bot; +https://ntfy.sh)"


def gnews(q):
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
            + "&hl=en-US&gl=US&ceid=US:en")


FEEDS = [
    ("Google News", gnews("(gold OR XAU OR bullion) when:1h")),
    ("Google News", gnews("(Fed OR Powell OR \"rate hike\" OR FOMC) when:1h")),
    ("Google News", gnews("(Iran OR Hormuz OR \"oil prices\" OR OPEC) when:1h")),
    ("Google News", gnews("(ISM OR CPI OR PCE OR payrolls OR jobless) US when:1h")),
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("Investing.com", "https://www.investing.com/rss/news_1.rss"),
    ("Investing.com", "https://www.investing.com/rss/news_11.rss"),
]

# หมวดข่าว: (ชื่อไทย, อีโมจิ, ความสำคัญ ntfy 1-5, คำค้น)
CATEGORIES = [
    ("เฟด/ดอกเบี้ย", "🏦", 5, r"\bpowell\b|\bfomc\b|rate hike|rate cut|hawkish|dovish|\bfed\b|federal reserve"),
    ("อิหร่าน/ฮอร์มุซ/สงคราม", "⚠️", 5, r"hormuz|\biran\b|israel|missile|strike|attack|war\b|tanker|vessel"),
    ("ตัวเลขเศรษฐกิจสหรัฐ", "📊", 4, r"\bism\b|\bcpi\b|\bpce\b|payroll|\bnfp\b|jobless|unemployment|\bgdp\b|inflation|retail sales|jolts"),
    ("น้ำมัน", "🛢️", 4, r"\boil\b|crude|brent|\bwti\b|opec"),
    ("ทองคำ", "🟡", 3, r"\bgold\b|xau|bullion|precious metal"),
    ("ดอลลาร์/บอนด์", "💵", 3, r"dollar|\bdxy\b|treasury yield|bond yield|yields"),
    ("หุ้นสหรัฐ", "📉", 3, r"nasdaq|s&p 500|wall street|stocks? (fall|slump|plunge|rally|surge)|sell-?off"),
]
# ต้องมีอย่างน้อย 1 คำที่ผูกกับทอง/มาโครจริงๆ จากแหล่งข่าวทั่วไป (FXStreet/Investing)
MUST_FOR_GENERAL = re.compile(
    r"\bgold\b|xau|bullion|\bfed\b|powell|fomc|hormuz|\biran\b|\boil\b|crude|\bism\b|\bcpi\b|\bpce\b|payroll|\bnfp\b|treasury yield|dollar index",
    re.I)
SKIP = re.compile(r"\bgold(en)? (coast|globe|state|retriever|medal|cup)\b|goldman|mining stock|exploration|drill(ing)? results|assay", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parse_items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        src = it.find("source")
        source = src.text.strip() if src is not None and src.text else ""
        yield title, link, pub, source


def to_dt(pub):
    try:
        d = email.utils.parsedate_to_datetime(pub)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def classify(title):
    hits = [(n, e, p) for n, e, p, pat in CATEGORIES if re.search(pat, title, re.I)]
    if not hits:
        return None
    names = [h[0] for h in hits][:2]
    return " + ".join(names), hits[0][1], max(h[2] for h in hits)


def norm_key(title):
    t = re.sub(r"\s+-\s+[^-]+$", "", title)          # ตัดชื่อสำนักข่าวท้ายหัวข้อ (Google News)
    t = re.sub(r"[^a-z0-9 ]", "", t.lower())
    return hashlib.sha1(" ".join(t.split()[:12]).encode()).hexdigest()[:16]


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    cutoff = time.time() - 3 * 86400                   # จำไว้ 3 วัน
    seen = {k: v for k, v in seen.items() if v > cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


def send(title, body, prio, tags, link):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"[DRY] P{prio} {title} | {body} | {link}")
        return
    # ใช้แบบ JSON เพราะหัวข้อมีภาษาไทย/อีโมจิ (ใส่ใน HTTP header ไม่ได้)
    payload = {"topic": NTFY_TOPIC, "title": title, "message": body,
               "priority": prio, "tags": [tags], "click": link}
    req = urllib.request.Request(
        NTFY_SERVER, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("ส่ง ntfy ไม่สำเร็จ:", e, file=sys.stderr)


def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = load_seen()
    now = datetime.now(timezone.utc)
    found = []
    for name, url in FEEDS:
        try:
            items = list(parse_items(fetch(url)))
        except Exception as e:
            print(f"ดึงข่าวจาก {name} ไม่ได้: {e}", file=sys.stderr)
            continue
        for title, link, pub, source in items:
            if not title or SKIP.search(title):
                continue
            if name != "Google News" and not MUST_FOR_GENERAL.search(title):
                continue
            cat = classify(title)
            if not cat:
                continue
            dt = to_dt(pub)
            if dt and (now - dt).total_seconds() > MAX_AGE_MIN * 60:
                continue
            key = norm_key(title)
            if key in seen:
                continue
            seen[key] = time.time()
            found.append((cat, title, link, dt, source or name))

    if first_run:
        print(f"รันครั้งแรก: จำข่าวไว้ {len(found)} ข่าว ไม่ส่ง (กันข่าวเก่าท่วม)")
        send("✅ บอทข่าวทองเริ่มทำงานแล้ว",
             f"เจอข่าวล่าสุด {len(found)} ข่าว (รอบแรกยังไม่ส่ง) ต่อไปข่าวใหม่จะเด้งทันทีที่บอทเช็ค",
             3, "white_check_mark", "https://ntfy.sh")
        found = []
    found.sort(key=lambda x: (-x[0][2], -(x[3].timestamp() if x[3] else 0)))
    for (cname, emoji, prio), title, link, dt, source in found[:MAX_PER_RUN]:
        tstr = dt.astimezone(timezone(timedelta(hours=7))).strftime("%H:%M") if dt else "--:--"
        send(f"{emoji} {cname}", f"{title}\n🕒 {tstr} น. · {source}", prio,
             "warning" if prio >= 5 else "newspaper", link)
        print("ส่งแล้ว:", cname, "|", title)
    save_seen(seen)
    print(f"เสร็จ: พบใหม่ {len(found)} ข่าว")


if __name__ == "__main__":
    main()
