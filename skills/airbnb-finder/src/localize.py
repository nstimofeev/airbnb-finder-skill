"""Page localisation without re-collecting anything: prices in the display currency, strings in the display language.

- Currency: the trip's search currency is converted at the ECB rate (frankfurter.app, free, no key);
  amounts written inside model texts ("€360", "360 €") are converted too.
- Language: every string shown on the page that isn't in the display language goes through one model pass;
  results are cached in data/07_<lang>.json by source string, so a re-render translates only what is new.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from .fetch import Fetcher
from .model import call
from .model_steps import language_name

log = logging.getLogger(__name__)
SYMBOL = {"EUR": "€", "USD": "$", "ILS": "₪", "GBP": "£", "JPY": "¥", "INR": "₹", "KRW": "₩", "TRY": "₺",
          "UAH": "₴", "PLN": "zł", "CHF": "CHF", "THB": "฿", "VND": "₫", "PHP": "₱", "BRL": "R$", "AUD": "A$",
          "CAD": "C$", "NZD": "NZ$", "MXN": "MX$", "CZK": "Kč", "HUF": "Ft", "SEK": "kr", "NOK": "kr", "DKK": "kr"}
# currency words a model may write instead of the symbol, per display language
WORDS = {"ru": {"EUR": "евро", "USD": "долл\\w*", "ILS": "шекел\\w*", "GBP": "фунт\\w*"},
         "en": {"EUR": "euros?", "USD": "dollars?", "ILS": "shekels?", "GBP": "pounds?"}}

# letters by script; a string with letters outside the display language's script surely needs translating
SCRIPTS = {"latin": r"A-Za-zÀ-ÖØ-öø-ɏ", "cyrillic": r"Ѐ-ӿ", "hebrew": r"֐-׿", "arabic": r"؀-ۿ",
           "greek": r"Ͱ-Ͽ", "cjk": r"぀-ヿ㐀-鿿가-힯", "georgian": r"Ⴀ-ჿ", "armenian": r"԰-֏", "thai": r"฀-๿"}
LANG_SCRIPT = {"ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic", "be": "cyrillic", "kk": "cyrillic",
               "he": "hebrew", "ar": "arabic", "fa": "arabic", "el": "greek", "ja": "cjk", "zh": "cjk", "ko": "cjk",
               "ka": "georgian", "hy": "armenian", "th": "thai"}
NON_RU_CYR = re.compile(r"[ђјљњћџЂЈЉЊЋЏіїєґІЇЄҐўЎ]")  # Serbian/Ukrainian/Belarusian letters Russian lacks


# ---- currency -----------------------------------------------------------------------------------
def fx_rate(f: Fetcher, src: str, dst: str) -> tuple[float, str]:
    d = f.get_json(f"https://api.frankfurter.app/latest?from={src}&to={dst}", f"fx_{src}_{dst}_{date.today()}")
    return float(d["rates"][dst]), d["date"]


def money_re(cur: str, lang: str = "en") -> re.Pattern:
    sym = re.escape(SYMBOL.get(cur, cur))
    num = r"\d[\d   ,.]*\d|\d"
    word = "|".join(w for w in (WORDS.get(lang, {}).get(cur), WORDS["en"].get(cur), re.escape(cur)) if w)
    return re.compile(rf"(?:{sym}|{cur})\s?(?P<a>{num})|(?P<b>{num})\s?(?:{sym}|{word})(?!\w)")


def convert_text(text: str, cur: str, rate: float, fmt, lang: str = "en") -> str:
    def sub(m):
        raw = (m.group("a") or m.group("b")).replace(" ", "").replace(" ", "").replace(" ", "")
        raw = raw.replace(",", "") if re.search(r",\d{3}(?!\d)", raw) else raw.replace(",", ".")
        try:
            return fmt(float(raw) * rate)
        except ValueError:
            return m.group(0)
    return money_re(cur, lang).sub(sub, text)


def money_fmt(cur: str, lang: str = "ru"):
    """English: '$1,235'; other languages: '1 235 ₪' (narrow no-break space groups, symbol after)."""
    sym = SYMBOL.get(cur, cur)
    if lang == "en":
        return lambda v: (sym if len(sym) <= 2 else sym + "\u00a0") + f"{round(v):,}"
    return lambda v: f"{round(v):,}".replace(",", "\u202f") + "\u00a0" + sym


# ---- translation --------------------------------------------------------------------------------
def needs_translation(s: str, lang: str) -> bool:
    if not s or not re.search(r"[^\W\d_]", s):
        return False
    s = s.replace("Airbnb", "")
    if lang == "ru":
        # other Cyrillic languages, or Latin words left in Russian text ("OSM", "warn", "No pets")
        return bool(NON_RU_CYR.search(s) or re.search(r"[A-Za-z]{3,}", s))
    script = LANG_SCRIPT.get(lang, "latin")
    other = "".join(v for k, v in SCRIPTS.items() if k != script)
    if re.search(f"[{other}]", s):
        return True
    if lang == "en":
        return bool(re.search(r"[À-ÖØ-öø-ɏ]", s))  # plain ASCII is taken as English (or a name)
    return True  # same script, language unknown: let the model decide, it returns the string as is


def still_foreign(src: str, got: str, lang: str) -> bool:
    """A translation that came back unchanged although it still looks foreign gets another try."""
    return lang == "ru" and got == src and needs_translation(got, lang)


TR_SCHEMA = {"type": "object", "required": ["items"], "additionalProperties": False,
             "properties": {"items": {"type": "array", "items": {
                 "type": "object", "required": ["i", "t"], "additionalProperties": False,
                 "properties": {"i": {"type": "integer"}, "t": {"type": "string"}}}}}}
TR_PROMPT = """Translate strings from a page for choosing a place to stay into {LANG}.

Rules:
- Ordinary text (amenities, house rules, cancellation policy, photo captions, guest reviews) — natural,
  short {LANG}, no bureaucratic wording. Translate review quotes close to the meaning, keeping the tone;
  keep a leading year like "(2024)".
- Listing titles: translate the meaning ("Log Cabin Petra in the National Park" → the {LANG} for that).
- Proper names (restaurants, villages, mountains, viewpoints, hosts' names): keep them recognisable; if {LANG}
  uses a different script, transliterate them the conventional way. Generic words inside names are translated
  ("Vidikovac Banjska stena" → "<{LANG} for viewpoint> Banjska Stena").
- Times like 4:00 PM → the {LANG} convention. Add nothing of your own.
- If a string is already in {LANG}, return it unchanged, except for words in other languages inside it:
  translate those ("OSM" → "map data", "ok" → "fine", "warn" → "uncertain", "fail" → "fails",
  "No pets" → "no pets", "pets_allowed=false" → "pets not allowed", "guest favorite" → "guest favourite").
  Leave "Airbnb" as is.

Answer: `items` — for every string its number `i` and the translation `t`.

## Strings
"""


def translate(strings: list[str], lang: str, cache_path: Path, cfg: dict, log_dir: Path,
              force: set[str] = frozenset()) -> dict[str, str]:
    """`force`: strings known to be foreign whatever they look like (quotes the model annotated)."""
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [s for s in dict.fromkeys(strings)
            if (s in force or needs_translation(s, lang)) and (s not in cache or still_foreign(s, cache[s], lang))]
    m = cfg.get("models") or {}
    head = TR_PROMPT.replace("{LANG}", language_name(lang))
    for n, i in enumerate(range(0, len(todo), 250)):
        chunk = todo[i:i + 250]
        prompt = head + "\n".join(json.dumps({"i": k, "s": s}, ensure_ascii=False) for k, s in enumerate(chunk))
        res = call(prompt, TR_SCHEMA, model=m.get("translate", "sonnet"), log_dir=log_dir, name=f"04_translate_{lang}_{n}")
        got = {it["i"]: it["t"] for it in res["items"] if 0 <= it["i"] < len(chunk)}
        for k, s in enumerate(chunk):
            if k in got and got[k].strip():
                cache[s] = got[k].strip()
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
        log.info("translate: %d/%d strings", min(i + 250, len(todo)), len(todo))
    return cache


QUOTE_CORE = re.compile(r"\s*\[[^\]]*\]\s*$")


def localize(data: dict, cfg: dict, data_dir: Path, f: Fetcher) -> dict:
    loc = cfg.get("display") or {}
    lang = (loc.get("language") or "en").lower()
    data["lang"] = lang
    # -- currency
    src = data["trip"].get("currency") or "USD"
    dst = loc.get("currency") or src
    if dst != src:
        rate, day = fx_rate(f, src, dst)
        conv = lambda v: None if v is None else round(v * rate)
        data["trip"]["max_total"] = conv(data["trip"].get("max_total") or None)
        for L in data["listings"]:
            L["price_total"], L["price_per_night"] = conv(L["price_total"]), conv(L["price_per_night"])
            L["currency"] = dst
        data["trip"]["currency"] = dst
        data["trip"]["fx"] = {"from": src, "to": dst, "rate": rate, "date": day}
        log.info("prices: %s → %s at %.4f (ECB %s)", src, dst, rate, day)
    data = _translate_all(data, cfg, data_dir, lang)
    if dst != src:
        _map_text(data, lambda t: convert_text(t, src, data["trip"]["fx"]["rate"], money_fmt(dst, lang), lang))
    return data


def _map_text(data: dict, fn) -> None:
    """Apply fn to every human-readable text on the page (amounts can hide in any of them)."""
    data["overview"] = fn(data["overview"] or "")
    for L in data["listings"]:
        for k in ("headline", "summary", "cancellation_policy", "title"):
            if isinstance(L.get(k), str):
                L[k] = fn(L[k])
        for k in ("pros", "cons", "red_flags", "house_rules", "safety_and_property", "overview"):
            L[k] = [fn(x) if isinstance(x, str) else x for x in L.get(k) or []]
        for a in L.get("amenities") or []:
            if a.get("note"):
                a["note"] = fn(a["note"])
        for c in L["criteria"].values():
            c["note"] = fn(c.get("note") or "")
            for q in c.get("quotes") or []:
                q["text"] = fn(q["text"])


def _translate_all(data: dict, cfg: dict, data_dir: Path, lang: str) -> dict:
    # -- strings to translate
    S: list[str] = []
    force: set[str] = set()  # quotes with a "[translation]" note: the assess step saw a foreign language
    add = lambda s: S.append(s) if isinstance(s, str) and s.strip() else None
    add(data.get("overview"))
    for c in data.get("criteria") or []:
        add(c.get("label"))
    for a in data["anchors"]:
        add(a.get("name"))
    for p in data.get("places") or []:
        add(p.get("name"))
    for L in data["listings"]:
        for k in ("title", "property_type", "cancellation_policy", "headline", "summary"):
            add(L.get(k))
        for c in L["criteria"].values():
            add(c.get("note"))
        for k in ("overview", "house_rules", "safety_and_property", "pros", "cons", "red_flags"):
            for x in L.get(k) or []:
                add(x)
        for a in L.get("amenities") or []:
            add(a.get("name")), add(a.get("category")), add(a.get("note"))
        add((L.get("host") or {}).get("response_time"))
        add((L.get("host") or {}).get("name"))
        for p in L.get("photos") or []:
            add(p.get("caption")), add(p.get("room"))
        for c in L["criteria"].values():
            for q in c.get("quotes") or []:
                core = QUOTE_CORE.sub("", q["text"])
                add(core)
                if core != q["text"]:
                    force.add(core)
        ctx = L.get("context") or {}
        for p in (ctx.get("food") or {}).get("nearest") or []:
            add(p.get("name"))
        for p in (ctx.get("nature") or {}).get("nearest") or []:
            add(p.get("name"))
        for n in (ctx.get("places_min") or {}):
            add(n)
        acc = ctx.get("access") or {}
        add(acc.get("to"))
    tr = translate(S, lang, data_dir / f"07_{lang}.json", cfg, data_dir / "model_logs", force)
    t = lambda s: tr.get(s, s) if isinstance(s, str) else s

    # -- apply
    data["overview"] = t(data.get("overview"))
    for c in data.get("criteria") or []:
        c["label"] = t(c.get("label"))
    for a in data["anchors"]:
        a["label"] = t(a["name"])  # name stays: distances_m is keyed by it
    for p in data.get("places") or []:
        p["name"] = t(p["name"])
    for L in data["listings"]:
        for k in ("title", "property_type", "cancellation_policy", "headline", "summary"):
            L[k] = t(L.get(k))
        for c in L["criteria"].values():
            c["note"] = t(c.get("note"))
        for k in ("overview", "house_rules", "safety_and_property", "pros", "cons", "red_flags"):
            L[k] = [t(x) for x in L.get(k) or []]
        for a in L.get("amenities") or []:
            a["name"], a["category"] = t(a.get("name")), t(a.get("category"))
            if a.get("note"):
                a["note"] = t(a["note"])
        h = L.get("host") or {}
        h["response_time"], h["name"] = t(h.get("response_time")), t(h.get("name"))
        for p in L.get("photos") or []:
            p["caption"], p["room"] = t(p.get("caption")), t(p.get("room"))
        for c in L["criteria"].values():
            for q in c.get("quotes") or []:
                core = QUOTE_CORE.sub("", q["text"])
                tt = t(core)
                if tt != core:
                    q["orig"], q["text"] = core, tt
        ctx = L.get("context") or {}
        for p in (ctx.get("food") or {}).get("nearest") or []:
            p["name"] = t(p.get("name"))
        for p in (ctx.get("nature") or {}).get("nearest") or []:
            p["name"] = t(p.get("name"))
        if ctx.get("places_min"):
            ctx["places_min"] = {t(k): v for k, v in ctx["places_min"].items()}
        if ctx.get("access"):
            ctx["access"]["to"] = t(ctx["access"].get("to"))
    return data
