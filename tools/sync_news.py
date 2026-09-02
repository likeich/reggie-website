"""
Cache army.mil news into Supabase.

api.army.mil sits behind Akamai, which returns 403 to any request carrying a
browser User-Agent and separately rejects Cloud Run's egress range regardless
of User-Agent. So the wasm client cannot call it and neither can our /proxy.
This job fetches it from a GitHub Actions runner and writes the result to a
table the client already has access to.

    SUPABASE_SERVICE_KEY=... python sync_news.py            # dry run
    SUPABASE_SERVICE_KEY=... python sync_news.py --apply

The scheduled job that runs this lives in likeich/reggie-website (tools/), not
here: that repo is public and public repos get unlimited Actions minutes.

This file is the source. The release workflow copies it there on every publish,
so the copy is a build output and must not be edited by hand -- which is what
went wrong before: the two were kept in step by a note in this docstring, the
note was not enough, and the copy sat 277 lines behind knowing nothing about
DVIDS while four services' news went unsynced.
"""
import argparse
import os
import email.utils
import re
from xml.etree import ElementTree
import sys

import requests

LEADS_URL = os.getenv('LEADS_URL', 'https://api.army.mil/api/v1/leads')
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://ziftzxigjayekmvvopkf.supabase.co')

# Must match Kotlin's Lead, which has no @SerialName annotations - the property
# names are the JSON keys. ga_id is the only nullable one. Pinned by
# test_supabase_contract.py, which reads the required set out of the Kotlin.
REQUIRED = ('id', 'title', 'short_title', 'body', 'url', 'page_url', 'author',
            'date', 'last_updated', 'short_description', 'description',
            'section', 'category', 'keywords', 'image')
OPTIONAL = ('ga_id',)


BUCKET = 'news-images'


def storage_url(name):
    return f'{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}'


def upload_image(key, url, session=None):
    """Mirror a lead image into Supabase storage, returning its public URL.

    The images live on api.army.mil alongside the leads, so they are blocked
    for the browser and for our Cloud Run proxy exactly the same way. Leaving
    the upstream URL in the row means every image 403s. Returns None if the
    image cannot be fetched, which drops the story rather than rendering it
    broken.
    """
    get = (session or requests).get
    try:
        r = get(url, headers={'accept': 'image/*'}, timeout=120)
        if r.status_code != 200 or not r.content:
            return None
        ext = 'png' if r.content[:8].startswith(b'\x89PNG') else 'jpg'
        name = f'{key}.{ext}'
        put = requests.post(
            f'{SUPABASE_URL}/storage/v1/object/{BUCKET}/{name}',
            headers={'apikey': os.environ['SUPABASE_SERVICE_KEY'],
                     'Authorization': f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
                     'Content-Type': r.headers.get('Content-Type', 'image/jpeg'),
                     'x-upsert': 'true'},
            data=r.content, timeout=120)
        if put.status_code >= 300:
            print(f'  WARN image upload {name}: {put.status_code} {put.text[:120]}')
            return None
        return storage_url(name)
    except Exception as e:
        print(f'  WARN image fetch {url[-40:]}: {type(e).__name__}')
        return None


# Scoped by branch, not by unit search.
#
# /rss/unit/USMC is a search feed and returns whatever is tagged that way --
# "U.S. Army Soldiers Conduct Operations in the Middle East" came back third,
# which is exactly the confusion having services at all is meant to remove.
# /rss/branch/Marines is the Marine Corps: 53 of 54 items mention no other
# service, and the one that does is a joint story.
#
# Keyed by the canonical slug, the same spelling branch_canonical() uses. A
# feed written under the old slug would insert rows the prune below cannot
# reach -- it scopes its delete by branch, so 'marine-corps' rows and 'usmc'
# rows are two sets that never clean each other up, and the News tab would
# show every story twice.
DVIDS_FEEDS = {
    'usmc': 'https://www.dvidshub.net/rss/branch/Marines',
    'uscg': 'https://www.dvidshub.net/rss/branch/Coast%20Guard',
    'ussf': 'https://www.dvidshub.net/rss/branch/Space%20Force',
    'usaf': 'https://www.dvidshub.net/rss/branch/Air%20Force',
}

# What a service used to be called, so a slug from an older script or an old
# invocation still lands on the rows it means. 'navy' stays mapped even though
# the service is gone: rows written under it should still resolve to one
# spelling if anything ever reads them.
LEGACY_BRANCHES = {
    'army': 'usa', 'navy': 'usn', 'marine-corps': 'usmc',
    'air-force': 'usaf', 'space-force': 'ussf', 'coast-guard': 'uscg',
}


def canonical_branch(slug):
    """The one spelling of a service, matching branch_canonical() in the
    database and Branch.slug in the client."""
    s = (slug or '').strip().lower()
    return LEGACY_BRANCHES.get(s, s)

# How many stories to carry per service. The Army endpoint decides its own;
# this is a feed of 400+ and the News tab shows a handful.
DVIDS_LIMIT = int(os.getenv('DVIDS_LIMIT', '40'))

# "6th Marine Regiment relief and appointment ceremony [Image 6 of 31]" -- one
# story, published once per photograph.
GALLERY_MARKER = re.compile(r'\s*\[Image \d+ of \d+\]\s*$', re.I)


def as_stored_date(value):
    """An RFC-822 pubDate in the format the Army's stories already use.

    DVIDS sends "Fri, 14 Aug 2026 16:19:26 -0400"; army.mil sends
    "2026-08-25 11:10:42", and the client parses the second. Stored verbatim
    the first threw DateTimeFormatException in the app, and one unparseable
    row emptied the whole News tab -- four services had a blank News screen
    from the day their news was first synced, while the rows sat correct in
    the database.

    A date that cannot be read becomes empty rather than a guess. A story with
    no date still reads; a story with an invented one is wrong on the screen.
    """
    if not value:
        return ''
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ''
    if parsed is None:
        return ''
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def og_image(html):
    """The first og:image on an article page, or None.

    The DVIDS feed carries no images at all -- 0 of 407 items -- and the news
    cards are built around one, so it has to come from the page. None means
    drop the story, the same rule the Army images follow: a card with a broken
    picture is worse than one story fewer.
    """
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                  html or '', re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                      html or '', re.I)
    return m.group(1) if m else None


def dvids_leads(xml_text, limit=None):
    """The feed's items, projected onto the shape news_row expects.

    Returns [] for anything unparseable rather than raising: a truncated or
    rejected download has to look like "no news", not like a crash that takes
    the Army sync down with it in the same job.
    """
    try:
        root = ElementTree.fromstring(xml_text or '')
    except ElementTree.ParseError:
        return []

    def text(item, tag):
        node = item.find(tag)
        return (node.text or '').strip() if node is not None else ''

    leads = []
    seen = set()
    cap = limit or DVIDS_LIMIT
    for item in root.findall('.//item'):
        if len(leads) >= cap:
            break
        # "news:573607" -- the number is the story, and Lead.id is an Int.
        digits = re.sub(r'\D', '', text(item, 'guid'))
        if not digits:
            continue
        # DVIDS publishes a photo gallery as one item per photograph, all
        # sharing a title and differing only in "[Image 6 of 31]". The Marine
        # Corps News tab shipped with four of them, reading as four separate
        # stories about the same ceremony.
        title = GALLERY_MARKER.sub('', text(item, 'title')).strip()
        # The Coast Guard feed goes further: five consecutive items carry one
        # title exactly. Whichever came first is the freshest telling, since
        # the feed is newest first.
        key = ' '.join(title.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        body = text(item, 'description')
        link = text(item, 'link')
        when = text(item, 'pubDate')
        leads.append({
            'id': int(digits),
            'title': title,
            'short_title': title,
            'body': body,
            'url': link,
            'page_url': link,
            'author': text(item, 'author') or 'DVIDS',
            'date': as_stored_date(when),
            'last_updated': as_stored_date(when),
            'short_description': body[:200],
            'description': body,
            'section': 'News',
            'category': 'News',
            'keywords': '',
            'image': '',
        })
    return leads


PRUNE_FLOOR = 0.5
SMALL_FEED = 4


def may_prune(fetched, cached):
    """Whether this feed is complete enough to delete against.

    DVIDS returned 6 Marine Corps stories where it had been returning 20. The
    prune deletes whatever it did not just fetch, so it took the other 14, and
    the run reported success. A short feed and 14 genuine removals look
    identical from here -- which is corpus_gate.py's whole argument, and this
    is the same script one table over.

    So the ambiguous case does nothing. The upsert still runs and the new
    stories still land; only the deletion waits for a feed big enough to
    believe. A feed that has really shrunk stays shrunk for one more run.

    A first sync has no cache to protect, and a handful of stories churns
    proportionally harder than a full one, so neither is held to the floor.
    """
    if cached <= 0 or cached < SMALL_FEED:
        return True
    return fetched >= cached * PRUNE_FLOOR


def cached_count(branch, key):
    """How many stories this service has right now, or None if we cannot tell.

    None means the prune is skipped: not knowing how much is there is not a
    licence to delete from it.
    """
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/news',
                         headers={'apikey': key, 'Authorization': f'Bearer {key}',
                                  'Prefer': 'count=exact', 'Range': '0-0'},
                         params={'select': 'id', 'branch': f'eq.{branch}'}, timeout=60)
        if r.status_code >= 300:
            return None
        return int(r.headers.get('content-range', '').split('/')[-1])
    except (requests.RequestException, ValueError):
        return None


def stale_ids(rows, branch, key):
    """The ids this run's prune would delete, or None if we cannot tell.

    Read-only, so the dry run can report exactly what --apply would remove.
    It could not before: `if not a.apply: return` sat above the prune
    entirely, so the preview listed five upserts and never mentioned that the
    same command would also delete -- which is the mistake CLAUDE.md records
    against sync_vector_store.py, whose preview called every file new and hid
    409 pending deletions.

    None for any failure, and the caller then skips rather than guesses. Not
    knowing what is there is not a licence to delete from it.
    """
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/news',
                         headers={'apikey': key, 'Authorization': f'Bearer {key}'},
                         params={'select': 'id', 'branch': f'eq.{branch}'}, timeout=60)
        if r.status_code >= 300:
            return None
        held = {str(row['id']) for row in r.json()}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    fetched = {str(row['id']) for row in rows}
    return sorted(held - fetched)


def prune_filter(rows, branch):
    """Which stories a sync may delete: this service's, minus what it just wrote.

    The branch clause is the whole point. Without it a Marine Corps run deletes
    every story it did not fetch -- all of the Army's -- and reports "DONE.
    cached 23 stories" while doing it. Same shape as stale_to_delete in
    sync_supabase.py; I fixed that one and did not think to look for a second.
    """
    keep = ','.join(str(row['id']) for row in rows)
    return {'id': f'not.in.({keep})', 'branch': f'eq.{branch}'}


def news_row(lead, image_url, branch='usa'):
    """Project one API lead onto our columns, or None if it cannot satisfy the
    client's model.

    Writing a row that is missing a non-nullable field would make
    kotlinx.serialization throw for the WHOLE list, so one malformed lead would
    blank the entire news screen. Skipping it costs one story instead.
    """
    if any(lead.get(k) is None for k in REQUIRED):
        return None
    if not image_url:
        return None
    row = {k: lead[k] for k in REQUIRED}
    # Which service's News tab this belongs on. Without it a Marine Corps
    # story lands on the Army's, which is the mistake publication_tables
    # already made once today.
    row['branch'] = branch
    for k in OPTIONAL:
        row[k] = lead.get(k)
    row['image'] = image_url          # self-hosted, not api.army.mil
    return row


def report_prune(rows, branch, key):
    """Say what the prune would do, without doing any of it.

    Called from the dry run, and it asks the same two questions the real run
    asks in the same order, so the preview cannot say one thing and the apply
    do another.
    """
    cached = cached_count(branch, key)
    if cached is None:
        print(f'  would skip the prune: could not count {branch} stories, '
              'and an unknown cache is not one to delete from')
        return
    if not may_prune(len(rows), cached):
        print(f'  would skip the prune: the feed returned {len(rows)} where '
              f'{cached} are cached, which is a short fetch as readily as '
              f'{cached - len(rows)} removals')
        return
    stale = stale_ids(rows, branch, key)
    if stale is None:
        print('  would skip the prune: could not read the cached ids')
        return
    print(f'  would prune {len(stale)} stale {branch} stories'
          + (f': {stale[:8]}' if stale else ''))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--branch', default='usa',
                    help="which service's news to sync: usa, or any of "
                         + ', '.join(sorted(DVIDS_FEEDS)))
    a = ap.parse_args(argv)
    # Accept the old spelling and write the new one, so an old invocation
    # cannot create a second, un-prunable set of rows.
    a.branch = canonical_branch(a.branch)

    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not key:
        sys.exit('SUPABASE_SERVICE_KEY not set')

    if a.branch == 'usa':
        # Deliberately no browser User-Agent: that is what Akamai rejects.
        r = requests.get(LEADS_URL, headers={'accept': 'application/json'}, timeout=120)
        if r.status_code != 200:
            sys.exit(f'army.mil returned {r.status_code} - refusing to touch the '
                     f'cached news rather than replace it with nothing')
        leads = r.json()
    else:
        # No service but the Army publishes a leads endpoint, so the rest come
        # from their DVIDS feed. Same rule on failure: an empty answer must
        # not be written over stories we already hold.
        feed = DVIDS_FEEDS.get(a.branch)
        if not feed:
            sys.exit(f'no news feed known for {a.branch}')
        r = requests.get(feed, timeout=120)
        if r.status_code != 200:
            sys.exit(f'dvids returned {r.status_code} - refusing to touch the '
                     f'cached news rather than replace it with nothing')
        leads = dvids_leads(r.text)
        if not leads:
            sys.exit('the feed parsed to nothing - refusing to touch the cached news')
        # The feed has no images; the article pages do.
        for lead in leads:
            try:
                page = requests.get(lead['url'], headers={'User-Agent': 'Mozilla/5.0'},
                                    timeout=60)
                lead['image'] = og_image(page.text) or ''
            except requests.exceptions.RequestException as e:
                print(f'  WARN image lookup {lead["id"]}: {type(e).__name__}')
                lead['image'] = ''
    if not a.apply:
        rows = [row for row in (news_row(x, storage_url(f'{x.get("id")}.jpg'), a.branch)
                                for x in leads) if row]
    else:
        rows = []
        for lead in leads:
            img = upload_image(lead.get('id'), lead.get('image') or '')
            row = news_row(lead, img, a.branch)
            if row:
                rows.append(row)
    skipped = len(leads) - len(rows)
    print(f'fetched {len(leads)} leads, {len(rows)} usable'
          + (f', {skipped} skipped for missing fields' if skipped else ''))

    if not rows:
        sys.exit('no usable leads - leaving the existing cache alone')
    if not a.apply:
        print('Dry run. Re-run with --apply.')
        for row in rows[:5]:
            print(f'  would upsert {row["id"]}  {row["title"][:60]}')
        report_prune(rows, a.branch, key)
        return 0

    h = {'apikey': key, 'Authorization': f'Bearer {key}',
         'Content-Type': 'application/json',
         'Prefer': 'resolution=merge-duplicates,return=minimal'}
    branch = rows[0].get('branch', 'usa') if rows else 'usa'
    resp = requests.post(f'{SUPABASE_URL}/rest/v1/news', headers=h, json=rows, timeout=120)
    if resp.status_code >= 300:
        sys.exit(f'upsert failed {resp.status_code}: {resp.text[:300]}')

    # Drop stories no longer carried upstream, so the screen matches the feed.
    #
    # Scoped to the branch being synced. Without `branch=eq.`, a Marine Corps
    # run deletes every story it did not just fetch -- which is all of the
    # Army's -- and reports "DONE. cached 23 stories" while doing it. That is
    # exactly the shape of stale_to_delete in sync_supabase.py, and I did not
    # think to look for a second one.
    #
    # Asking for the deleted rows back, rather than firing and hoping. A prune
    # that removed more than it should used to leave no trace at all: the run
    # printed "DONE. cached 21 stories" whether it had deleted one row or every
    # row in the table. 103 stories went missing between two checks here and
    # the logs could not say what took them, because nothing recorded a count.
    cached = cached_count(branch, key)
    if cached is None:
        print(f'  skipping prune: could not count {branch} stories, '
              'and an unknown cache is not one to delete from')
        print(f'DONE. cached {len(rows)} stories')
        return
    if not may_prune(len(rows), cached):
        print(f'  skipping prune: the feed returned {len(rows)} where {cached} are '
              f'cached, which is a short fetch as readily as {cached - len(rows)} '
              'removals -- the new stories are saved, the old ones stay')
        print(f'DONE. cached {len(rows)} stories')
        return

    prune_headers = {k: v for k, v in h.items() if k != 'Prefer'}
    prune_headers['Prefer'] = 'return=representation'
    d = requests.delete(f'{SUPABASE_URL}/rest/v1/news',
                        headers=prune_headers,
                        params=prune_filter(rows, branch), timeout=120)
    if d.status_code >= 300:
        print(f'  WARN prune failed {d.status_code}: {d.text[:160]}')
    else:
        try:
            gone = d.json()
        except ValueError:
            gone = []
        other = sorted({r.get('branch') for r in gone} - {branch})
        print(f'  pruned {len(gone)} stale {branch} stories')
        if other:
            # Cannot happen through prune_filter, which scopes by branch. If it
            # ever prints, the filter is not doing what it claims and that is
            # worth a loud line rather than a silent hole in another service.
            print(f'  WARNING: the prune also removed rows from {other}')
    print(f'DONE. cached {len(rows)} stories')
    return 0


if __name__ == '__main__':
    sys.exit(main())
