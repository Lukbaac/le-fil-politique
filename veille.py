#!/usr/bin/env python3
"""Collecte les flux de sources.json, met à jour l'archive et génère le site Le Fil Politique.

Bibliothèque standard uniquement, aucune IA. Usage : python3 veille.py [--no-fetch]
"""
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
import html.parser
import http.client
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "articles.json"
STATUS = ROOT / "status.json"
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC).replace(microsecond=0)
KEEP_DAYS = 3
MAX_FEED_BYTES = 8_000_000
SUMMARY_LEN = 260
FEED_TEXT_LEN = 1500
ARTICLE_TEXT_LEN = 3000
MAX_IDEA_LEN = 320
MIN_IDEA_LEN = 110
SAME_STORY_WORDS = 3
SAME_STORY_SHARE = 0.34
STORY_STOPWORDS = set(
    "emmanuel macron president republique elysee premier ministre ministres gouvernement matignon assemblee nationale "
    "senat depute deputes deputee senateur senateurs politique politiques france francais francaise francaises "
    "contre apres avant pour avec dans plus cette comment leur leurs nouvelle nouveau sans sous selon direct "
    "veut fait faire etre avoir sera sont tout tous mais face entre aussi encore deja presidentielle election elections "
    "candidat candidate candidats 2025 2026 2027 2028 janvier fevrier mars avril juin juillet aout septembre octobre "
    "novembre decembre lundi mardi mercredi jeudi vendredi samedi dimanche semaine pourquoi quoi selon".split()
)
TOPIC_LABELS = {
    "matinales": "Matinales", "elysee": "Élysée & Matignon", "gouvernement": "Gouvernement",
    "parlement": "Parlement", "partis": "Partis politiques", "presidentielle": "Présidentielle 2027",
}
# Google Actualités écrit parfois le même média de plusieurs façons
PUBLISHER_NAMES = {"BFM": "BFMTV", "BFM TV": "BFMTV", "l'Opinion": "L'Opinion", "Euronews.com": "Euronews", "TF1INFO": "TF1 Info",
                   "20minutes": "20 Minutes", "lanouvellerepublique.fr": "La Nouvelle République", "Public Senat": "Public Sénat",
                   "Le Journal du Dimanche": "Le JDD", "JDD": "Le JDD", "HuffPost": "Le HuffPost", "France Info": "franceinfo", "RTL.fr": "RTL"}
USER_AGENT = "Mozilla/5.0 (Le Fil Politique, veille RSS)"
BROWSER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
PAGE_META = (
    '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    '<meta name="robots" content="noindex">'
    '<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon.svg" type="image/svg+xml">'
    '<link rel="apple-touch-icon" href="icon-180.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="Le Fil">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="theme-color" content="#172036"></head><body>'
)


def fold(text):
    """Minuscule-insensible géré à part : retire seulement accents et apostrophes typographiques."""
    text = unicodedata.normalize("NFKD", text.replace("’", "'"))
    return "".join(c for c in text if not unicodedata.combining(c))


def compile_terms(terms):
    """Un terme tout en majuscules (RN, LFI, PS…) n'est reconnu qu'en majuscules."""
    exact = [re.escape(fold(t)) for t in terms if t.isupper()]
    loose = [re.escape(fold(t)) for t in terms if not t.isupper()]
    patterns = []
    if exact:
        patterns.append(re.compile(r"(?<!\w)(?:" + "|".join(exact) + r")(?!\w)"))
    if loose:
        patterns.append(re.compile(r"(?<!\w)(?:" + "|".join(loose) + r")(?!\w)", re.I))
    return patterns


def matches(patterns, text):
    return any(p.search(text) for p in patterns)


def clean(text, limit=None):
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = html.unescape(html.unescape(re.sub(r"<[^>]+>", " ", text)))  # certains flux encodent deux fois
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
    return text


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return min(parsed.astimezone(UTC), NOW)


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node, *names):
    for child in node:
        if local(child.tag) in names and (child.text or "").strip():
            return child.text
    return ""


def entry_link(node):
    for child in node:
        if local(child.tag) != "link":
            continue
        if child.get("href") and child.get("rel", "alternate") == "alternate":
            return child.get("href")
        if (child.text or "").strip():
            return child.text.strip()
    return ""


def fetch(source):
    request = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    data = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("flux trop volumineux")
    if b"<!ENTITY" in data[:4096]:
        raise ValueError("entités XML refusées")
    entries = []
    for node in ET.fromstring(data).iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(node, "title"))
        link = entry_link(node)
        name = source["name"]
        if name == "Google Actualités":
            publisher = child_text(node, "source").strip()
            if publisher:
                name = PUBLISHER_NAMES.get(publisher, publisher)
                title = re.sub(r"\s+-\s+" + re.escape(publisher) + r"$", "", title)
            summary = ""
        else:
            summary = clean(child_text(node, "description", "summary", "content", "encoded"), FEED_TEXT_LEN)
        title = re.sub(r"^(EN DIRECT|DIRECT)[.,:\s-]+", "", title)
        if title and link.startswith(("http://", "https://")):
            date = parse_date(child_text(node, "pubdate", "published", "updated", "date"))
            entries.append({"title": title, "link": link, "summary": summary, "date": date, "source": name})
    return entries


class ParagraphParser(html.parser.HTMLParser):
    """Récupère les paragraphes d'une page, en privilégiant ceux du corps de l'article."""

    SKIP = {"script", "style", "nav", "footer", "aside", "form", "header"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = self.main = 0
        self.current = None
        self.in_main, self.others = [], []
        self.description = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (attrs.get("property") == "og:description" or attrs.get("name") == "description"):
            self.description = self.description or (attrs.get("content") or "").strip()
        elif tag in self.SKIP:
            self.skip += 1
        elif tag in ("article", "main"):
            self.main += 1
        elif tag == "p" and not self.skip:
            self.current = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("article", "main"):
            self.main = max(0, self.main - 1)
        elif tag == "p" and self.current is not None:
            text = re.sub(r"\s+", " ", "".join(self.current)).strip()
            if len(text) >= 60 and "�" not in text:  # paragraphe mal encodé par le site
                (self.in_main if self.main else self.others).append(text)
            self.current = None

    def handle_data(self, data):
        if self.current is not None and not self.skip:
            self.current.append(data)


def article_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        page = response.read(MAX_FEED_BYTES).decode(charset, "replace")
    parser = ParagraphParser()
    parser.feed(page)
    # Le chapô annoncé aux réseaux sociaux sert de repli quand le texte est réservé aux abonnés
    return "\n".join(parser.in_main or parser.others or [parser.description])[:ARTICLE_TEXT_LEN]


def google_link(link):
    """Retrouve l'adresse réelle d'un article de Google Actualités, comme le fait la page de Google."""
    gid = urllib.parse.urlparse(link).path.rsplit("/", 1)[-1]
    request = urllib.request.Request(f"https://news.google.com/articles/{gid}", headers={"User-Agent": BROWSER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        page = response.read(MAX_FEED_BYTES).decode("utf-8", "replace")
    signature = re.search(r'data-n-a-sg="([^"]+)"', page)
    timestamp = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (signature and timestamp):
        return link
    query = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{timestamp.group(1)},"{signature.group(1)}"]']
    request = urllib.request.Request(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=("f.req=" + urllib.parse.quote(json.dumps([[query]]))).encode(),
        headers={"User-Agent": BROWSER_AGENT, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        found = re.search(r'\[\\"garturlres\\",\\"(https?://[^"\\]+)\\"', response.read().decode("utf-8", "replace"))
    return found.group(1) if found else link


def safe_article_text(article):
    try:
        if article["link"].startswith("https://news.google.com/"):
            article["link"] = google_link(article["link"])  # le lien ouvre aussi directement l'article
        if article["link"].startswith("https://news.google.com/"):
            return ""
        return article_text(article["link"])
    except (OSError, ValueError, http.client.HTTPException):
        return ""


def main_idea(title, text):
    """Garde les premières phrases complètes qui disent autre chose que le titre. Gratuit : aucune IA."""
    text = re.sub(r"^Par [^,]{3,80}, le \w+ \d{1,2} \w+ \d{4} à \s*\d{1,2}:\d{2}\s*", "", text.strip())  # signature LCP
    text = re.sub(r"^(Lire|Voir) (le communiqué|l'interview|la vidéo|le discours)\.?\s*$", "", text)  # aperçus de l'Élysée
    text = re.sub(r"\s*The post .*$", "", text, flags=re.S)
    text = re.sub(r"\s*(\(\.\.\.\)|\[(…|\.\.\.)\]|…|\.\.\.)\s*$", "", text)
    sentences = re.split(r"(?<=[.!?…»])\s+(?=[A-ZÀ-Ý«\"])", text)
    if sentences and not re.search(r"[.!?»\"]$", sentences[-1]):
        sentences.pop()  # phrase coupée par le site source
    title_words = set(re.findall(r"\w{4,}", fold(title).lower()))
    idea = ""
    for sentence in sentences:
        words = set(re.findall(r"\w{4,}", fold(sentence).lower()))
        if not idea and words and len(words & title_words) / len(words) > 0.8:
            continue  # simple reprise du titre
        if idea and len(idea) + len(sentence) > MAX_IDEA_LEN:
            break
        idea = f"{idea} {sentence}".strip()
        if len(idea) >= MIN_IDEA_LEN:
            break
    if len(idea) > MAX_IDEA_LEN:
        idea = idea[:MAX_IDEA_LEN].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
    return idea


def add_idea(article):
    """L'aperçu du flux suffit le plus souvent ; sinon on lit le début de la page de l'article."""
    feed_text = article["summary"]
    idea = main_idea(article["title"], feed_text)
    if len(idea) < MIN_IDEA_LEN:
        page_idea = main_idea(article["title"], safe_article_text(article))
        idea = page_idea if len(page_idea) > len(idea) else idea
    if not idea and len(feed_text) > 40:
        idea = clean(re.sub(r"\s*\((\.\.\.|…)\)\s*$", "…", feed_text), MAX_IDEA_LEN)
    article["summary"] = idea


def story_words(title):
    """Les mots qui distinguent une affaire : Nuñez, lycéens, Livret… sans le vocabulaire commun à toute la politique."""
    return set(re.findall(r"\w{4,}", fold(title).lower())) - STORY_STOPWORDS


UPPER = "A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜ"
LOWER = "a-zàâäçéèêëîïôöùûüÿœæñ"
NAME_WORD = rf"[{UPPER}][{LOWER}]+(?:-[{UPPER}][{LOWER}]+)?"
FULL_NAME = rf"{NAME_WORD}(?:(?:\s+(?:de|du|des)\s+|\s+d['’]|\s+){NAME_WORD}){{1,3}}"
# Mots à majuscule qui ouvrent un titre sans être un prénom : « Présidentielle 2027 : … », « Colère lycéenne : … »
NOT_FIRST_NAMES = set(
    "presidentielle budget colere violences commission prix liberation mobilisation mobilisations agression livreurs carburants "
    "surpopulation maintien midterms avant avec apres pour selon chez les la le l france paris assemblee senat matignon elysee "
    "attentat primaire gouvernement ministre president presidente premier premiere depute deputee senateur senatrice parti "
    "rassemblement front nouveau ensemble union place republicains frontieres edito interview direct exclusif podcasts info "
    "sondage reforme loi projet proposition marche greve crise affaire ecole justice police europe horizons renaissance "
    "modem reconquete ecologistes insoumis insoumise socialistes communistes republique pourquoi comment quand quels quelles "
    "benefices economie interieur sante education travail culture armees agriculture logement transports commerce "
    "industrie numerique outre-mer finances comptes".split()
)
SPEECH_VERBS = (
    "assure|assurent|estime|affirme|déclare|dénonce|appelle|annonce|reconnaît|reconnait|confirme|juge|note|rappelle|indique|"
    "ironise|fustige|accuse|critique|répond|réagit|tacle|défend|promet|prévient|plaide|souhaite|regrette|salue|insurge|dit|redit|"
    "assume|veut|reproche|fait|propose|demande|alerte|admet|explique|insiste|martèle|lance|croit|renoncera|confie|raconte|exhorte|"
    "réclame|refuse|rejette|condamne|déplore|amuse|interpelle|exige|menace|attaque|étrille|raille|lâche|glisse|tranche|"
    "avertit|concède|prône|défie|justifie|minimise|répète|maintient|réaffirme|qualifie|compare|sort|s'engage|engage|charge|"
    "repousse|clame|assène|considère|constate|évoque|analyse|prédit|revendique|félicite|envisage|exclut|réfute|dément|conteste"
)
# Ce qui peut séparer un verbe de l'auteur des propos : « estime la députée LFI Aurélie Trouvé »
ROLE_WORDS = (
    r"(?:(?:le|la|l['’]|les|du|de|des|d['’])\s*|(?:ministre|ministres|député|députée|sénateur|sénatrice|porte-parole|président|"
    r"présidente|secrétaire|maire|candidat|candidate|chef|cheffe|patron|patronne|leader|ancien|ancienne|premier|première|"
    r"écologiste|insoumis|insoumise|socialiste|communiste|eurodéputé|eurodéputée|élu|élue|gouvernement|général|générale|"
    r"national|nationale|européen|européenne|délégué|déléguée|garde|sceaux)\s+|[A-Z]{2,5}\s+|«Q»\s+)"
)


def compile_rules(config):
    people, names = {}, set()
    for full in config.get("personnalites", []):
        parts = full.split()
        surname = " ".join(parts[2:] if len(parts) > 2 and parts[1] in ("de", "du") else parts[1:])
        people[fold(full).lower()] = full
        people.setdefault(fold(surname).lower(), full)  # « Darmanin » seul désigne Gérald Darmanin
        names |= {full, surname}
    known = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    name = rf"(?:{known}|{FULL_NAME})" if known else FULL_NAME
    verbs = rf"(?:ne\s+|n['’]|se\s+|s['’])?(?:{SPEECH_VERBS})\b"
    return {
        "topics": {t: compile_terms(config["keywords"][t]) for t in TOPIC_LABELS},
        "leaders": compile_terms(config["keywords"].get("partis_responsables", [])),
        "natures": {n: compile_terms(v) for n, v in config["natures"].items()},
        "people": people,
        "verbs": re.compile(rf"\b{verbs}"),
        "speaker": [
            re.compile(rf"({name})\s*:\s*«Q»"),  # Prisca Thévenot : « … »
            re.compile(rf"({name})(?:\s*\([^)]{{1,30}}\))?(?:,\s*[^,]{{3,70}},)?\s+{verbs}"),  # Gérald Darmanin, ministre…, dénonce
            re.compile(rf"(?:\b(?:{SPEECH_VERBS})|\bselon)\s+{ROLE_WORDS}{{0,6}}({name})"),  # …, estime la députée Prisca Thévenot
            re.compile(rf"\b[Pp]our\s+{ROLE_WORDS}{{0,6}}({name})\s*,"),  # pour Darmanin, il n'y a pas…
            re.compile(rf"(?:invitée?|interview|entretien|«Q»)\s+(?:de|d['’]|avec)\s+({name})"),  # le « 8h30 franceinfo » de Fabien Roussel
        ],
    }


def speaker_name(candidate, rules):
    """Nettoie un nom trouvé dans un titre : retire « Président » devant, écarte « Présidentielle 2027 », complète « Darmanin »."""
    words = candidate.split()
    while words and (fold(words[0]).lower().strip("'’") in NOT_FIRST_NAMES or not words[0][0].isupper()):
        words = words[1:]  # « Président Emmanuel Macron », « Bénéfices de Total »
    name = " ".join(words)
    if not name:
        return ""
    if fold(name).lower() in rules["people"]:
        return rules["people"][fold(name).lower()]
    return name if len(words) >= 2 else ""


def declaration(title, source, rules):
    """Type de déclaration et auteur des propos, lus dans le titre. Sans IA : des règles sur la forme des titres de presse."""
    natures = [n for n, terms in rules["natures"].items() if matches(terms, fold(title))]
    quotes = re.findall(r"«\s*([^»]+?)\s*»|[\"“]([^\"”]+)[\"”]", title)
    quoted = [a or b for a, b in quotes]
    text = re.sub(r"«[^»]*»|[\"“][^\"”]*[\"”]", "«Q»", title)  # les noms cités à l'intérieur des guillemets ne parlent pas
    speaker = ""
    for pattern in rules["speaker"]:
        for found in pattern.finditer(text):
            speaker = speaker_name(found.group(1), rules)
            if speaker:
                break
        if speaker:
            break
    if not speaker and source == "Élysée" and re.search(r"Président|Macron", title):
        speaker = "Emmanuel Macron"
    # Une vraie citation : des propos entre guillemets attribués à quelqu'un, pas un simple mot mis en valeur.
    long_quote = any(len(q.split()) >= 3 for q in quoted)
    if quoted and (speaker or (long_quote and rules["verbs"].search(text))):
        natures.insert(0, "Citation")
    return natures, speaker


def tag(article, rules):
    """Range un article dans ses rubriques ; refait à chaque collecte pour suivre les changements de sources.json."""
    text = fold(article["title"] + " " + article["summary"][:SUMMARY_LEN])
    topics = [t for t, terms in rules["topics"].items() if matches(terms, text)]
    if "partis" not in topics and "presidentielle" not in topics and matches(rules["leaders"], text):
        topics.append("partis")  # un chef de parti cité hors campagne présidentielle : c'est la vie des partis
    base = article.setdefault("base", [])
    if not article.setdefault("filtered", True) or topics:
        topics = list(dict.fromkeys(base + topics))
    if not topics:
        topics = [t for t in article.get("topics", []) if t in TOPIC_LABELS]  # article d'une version précédente
    article["topics"] = sorted(topics, key=list(TOPIC_LABELS).index)
    article["natures"], article["speaker"] = declaration(article["title"], article["source"], rules)
    return article["topics"]


def title_key(title):
    return re.sub(r"[^a-z0-9]+", "", fold(title).lower())[:90]


def same_story(words, date, archive):
    """Article déjà couvert : au moins 3 mots distinctifs en commun, soit un bon tiers des deux titres réunis,
    publié à moins de deux jours d'écart. Le tiers évite de fusionner tous les articles qui citent Mélenchon."""
    for article in archive:
        shared = len(words & article["words"])
        if (shared >= SAME_STORY_WORDS and shared >= SAME_STORY_SHARE * len(words | article["words"])
                and abs(parse_date(article["date"]) - date) < dt.timedelta(days=2)):
            return article
    return None


def collect(archive, config):
    rules = compile_rules(config)
    exclude_terms = compile_terms(config["exclude"])
    trusted = {fold(name).lower() for name in config["trusted_publishers"]}
    known_ids = {a["id"] for a in archive}
    known_titles = {title_key(a["title"]) for a in archive}
    for a in archive:
        a["words"] = story_words(a["title"])
    sources = config["sources"]
    statuses, added, new_articles = [], 0, []
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(fetch, s) for s in sources]
    for source, future in zip(sources, futures):
        try:
            entries = future.result()
        except Exception as exc:  # une source en panne ne bloque jamais les autres
            statuses.append({"name": source["name"], "ok": False, "error": str(exc)[:120]})
            continue
        new = 0
        for entry in entries:
            uid = hashlib.sha1(entry["link"].encode()).hexdigest()[:16]
            key = title_key(entry["title"])
            date = entry["date"] or NOW
            if uid in known_ids or key in known_titles or date < NOW - dt.timedelta(days=KEEP_DAYS):
                continue
            if source["name"] == "Google Actualités" and fold(entry["source"]).lower() not in trusted:
                continue
            if not source.get("official") and matches(exclude_terms, fold(entry["title"] + " " + entry["summary"][:SUMMARY_LEN])):
                continue
            article = {
                "id": uid, "title": entry["title"], "link": entry["link"], "summary": entry["summary"],
                "source": entry["source"], "date": date.isoformat(), "added": NOW.isoformat(),
                "official": bool(source.get("official")), "also": [],
                "base": source.get("topics", []), "filtered": bool(source.get("filter")),
            }
            if not tag(article, rules):
                continue
            words = story_words(entry["title"])
            # Une même déclaration reprise par plusieurs médias n'est affichée qu'une fois,
            # avec la liste des médias qui en parlent : c'est ce qui fait « L'essentiel ».
            if source.get("filter"):
                known = same_story(words, date, archive + new_articles)
                if known:
                    if entry["source"] != known["source"] and entry["source"] not in known["also"]:
                        known["also"].append(entry["source"])
                    continue
            article["words"] = words
            new_articles.append(article)
            known_ids.add(uid)
            known_titles.add(key)
            new += 1
        added += new
        statuses.append({"name": source["name"], "ok": True, "items": len(entries), "new": new})
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        list(pool.map(add_idea, new_articles))
    archive.extend(new_articles)
    for a in archive:
        del a["words"]
    return statuses, added


def score(article):
    points = 3 if article["official"] else 0
    points += 2 * min(len(article["also"]), 6)
    points += 2 if article.get("speaker") else 0
    points += sum({"Citation": 2, "Interview": 2, "Discours": 3, "Annonce": 1, "Réaction": 1}.get(n, 0) for n in article["natures"])
    return points + (1 if article["summary"] else 0)


def notification_brief(articles):
    """Contenu de la notification du matin : ce qui est arrivé depuis la précédente, le plus repris d'abord, sans IA."""
    since = NOW - dt.timedelta(hours=24)
    marker = ROOT / "last_push.txt"
    if marker.exists():
        since = max(since, parse_date(marker.read_text()))
    # "added" : une date de publication peut être antérieure à l'arrivée de l'article dans la veille
    recent = sorted((a for a in articles if parse_date(a.get("added", a["date"])) >= since), key=lambda a: (score(a), a["date"]), reverse=True)
    count = len(recent)
    if count:
        title = f"Le Fil Politique : {count} nouvelle{'s' if count > 1 else ''} actualité{'s' if count > 1 else ''}"
        body = "\n".join("• " + clean(a["title"], 62) for a in recent[:3])
    else:
        title, body = "Le Fil Politique", "Pas de nouvelle actualité depuis hier."
    (ROOT / "brief.json").write_text(json.dumps({"title": title, "body": body, "count": count}, ensure_ascii=False), encoding="utf-8")


def build(articles, statuses):
    for a in articles:
        a["score"] = score(a)
    payload = json.dumps(
        {"updated": NOW.isoformat(), "articles": articles, "sources": statuses, "labels": TOPIC_LABELS, "vapid": os.environ.get("VAPID_PUBLIC_KEY", "")},
        ensure_ascii=False, separators=(",", ":"),
    ).replace("<", "\\u003c")
    page = (ROOT / "template.html").read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    register_sw = "<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('sw.js').catch(function(){})</script>"
    (ROOT / "index.html").write_text(PAGE_META + page + register_sw + "</body></html>", encoding="utf-8")
    for a in articles:
        del a["score"]
    notification_brief(articles)


def main():
    config = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    archive = json.loads(ARCHIVE.read_text(encoding="utf-8")) if ARCHIVE.exists() else []
    if "--no-fetch" in sys.argv:
        statuses, added = (json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else []), 0
    else:
        statuses, added = collect(archive, config)
    rules = compile_rules(config)
    for a in archive:
        if "base" not in a:  # article d'une version précédente : rubriques d'office des sources officielles
            a["base"], a["filtered"] = (a["topics"], False) if a["official"] else ([], True)
        tag(a, rules)
    cutoff = NOW - dt.timedelta(days=KEEP_DAYS)
    archive = sorted((a for a in archive if parse_date(a["date"]) >= cutoff), key=lambda a: a["date"], reverse=True)
    ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=0), encoding="utf-8")
    STATUS.write_text(json.dumps(statuses, ensure_ascii=False, indent=0), encoding="utf-8")
    build(archive, statuses)
    failed = [s for s in statuses if not s["ok"]]
    print(f"{added} nouveaux, {len(archive)} en archive, {len(failed)} source(s) en échec sur {len(statuses)}")
    for s in failed:
        print(f"  ÉCHEC {s['name']} : {s['error']}")


if __name__ == "__main__":
    main()
