"""
Cache army.mil and DVIDS news into Supabase.

api.army.mil sits behind Akamai, which returns 403 to any request carrying a
browser User-Agent and separately rejects Cloud Run's egress range regardless
of User-Agent. So the wasm client cannot call it and neither can our /proxy.
This job fetches it from a GitHub Actions runner and writes the result to a
table the client already has access to. Every other service reads
api.dvidshub.net instead, which needs DVIDS_API_KEY.

    SUPABASE_SERVICE_KEY=... python sync_news.py                       # dry run, Army
    SUPABASE_SERVICE_KEY=... DVIDS_API_KEY=... python sync_news.py --branch usn --apply

The scheduled job that runs this lives in likeich/reggie-website (tools/), not
here: that repo is public and public repos get unlimited Actions minutes.

This file is the source. The release workflow copies it to
likeich/reggie-website on every publish -- the copy there is a build output
and must not be edited by hand. It must never carry a key literal: read both
keys from the environment only.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

import requests

LEADS_URL = os.getenv('LEADS_URL', 'https://api.army.mil/api/v1/leads')
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://ziftzxigjayekmvvopkf.supabase.co')

# Must match Kotlin's Lead verbatim -- no @SerialName annotations, so these
# are the JSON keys. ga_id is the only nullable one. See test_supabase_contract.py.
REQUIRED = ('id', 'title', 'short_title', 'body', 'url', 'page_url', 'author',
            'date', 'last_updated', 'short_description', 'description',
            'section', 'category', 'keywords', 'image')
OPTIONAL = ('ga_id',)


BUCKET = 'news-images'


def storage_url(name):
    return f'{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}'


def upload_image(key, url, session=None):
    """Mirror a lead image into Supabase storage, returning its public URL.

    api.army.mil blocks the browser the same way it blocks our proxy, so the
    upstream URL would 403 in the row. Returns None on failure, which drops
    the story rather than rendering it broken.
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


DVIDS_API_URL = 'https://api.dvidshub.net/search'

# The branch name api.dvidshub.net's `branch` parameter expects, not the RSS
# path segment -- they differ only in "Coast Guard"/"Space Force"/"Air Force"
# needing their space, which `requests` encodes for us.
#
# Keyed by the canonical slug: an entry written under a legacy slug would
# insert rows the prune below cannot reach, since it scopes its delete by
# branch.
DVIDS_BRANCHES = {
    'usn': 'Navy',
    'usmc': 'Marines',
    'uscg': 'Coast Guard',
    'ussf': 'Space Force',
    'usaf': 'Air Force',
}

# Legacy slugs, so an old invocation still lands on the rows it means.
LEGACY_BRANCHES = {
    'army': 'usa', 'navy': 'usn', 'marine-corps': 'usmc',
    'air-force': 'usaf', 'space-force': 'ussf', 'coast-guard': 'uscg',
}


def canonical_branch(slug):
    """The one spelling of a service, matching branch_canonical() in the
    database and Branch.slug in the client."""
    s = (slug or '').strip().lower()
    return LEGACY_BRANCHES.get(s, s)

# How many stories to carry per service. api.dvidshub.net's `type=news`
# filter alone returns 1000+ per branch, but getNews() in the client has no
# pagination -- every row synced here is fetched on every News tab open, so
# this stays well short of that ceiling rather than chasing it. 60 is a
# comfortable scroll's worth per service and keeps five services' worth of
# rows small against Supabase's free-tier row and bandwidth limits.
DVIDS_LIMIT = int(os.getenv('DVIDS_LIMIT', '60'))

# DVIDS publishes a photo gallery as one item per photograph, e.g.
# "... ceremony [Image 6 of 31]" -- one story, not thirty-one.
GALLERY_MARKER = re.compile(r'\s*\[Image \d+ of \d+\]\s*$', re.I)


def as_stored_date(value):
    """DVIDS' ISO-8601 date, in the format the Army's stories already use.

    DVIDS sends "2026-08-25T11:10:42Z"; army.mil sends "2026-08-25 11:10:42",
    and the client parses only the second -- storing the first verbatim
    throws DateTimeFormatException and blanks the whole News tab. A date that
    cannot be read becomes empty rather than a guess.
    """
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return ''
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


# DVIDS' search API answers `thumbnail` at a fixed 122x92 -- a contact sheet
# size, not a card image. The CDN serves the same asset at other sizes from
# the same path, and DVIDS' own og:image uses 1000w, so ask for that. Falls
# back to whatever was given if the path is not the shape we expect: a
# smaller picture beats none.
THUMB_SIZE = re.compile(r'/\d+x\d+(_q\d+\.[a-z]+)$')


def full_size(thumbnail):
    """A card-sized version of a DVIDS thumbnail URL, or it unchanged."""
    if not thumbnail:
        return ''
    return THUMB_SIZE.sub(r'/1000w\1', thumbnail)


def og_image(html):
    """The first og:image on an article page, or None.

    Most api.dvidshub.net results carry their own `thumbnail`; this is the
    fallback for the minority that do not (roughly half of some services'
    results, e.g. the Coast Guard, in a spot check). None means drop the
    story: a card with a broken picture is worse than one story fewer.
    """
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                  html or '', re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                      html or '', re.I)
    return m.group(1) if m else None


# api.dvidshub.net's `type=news` parameter already excludes photos, video,
# webcasts, audio and base newspapers server-side -- that used to be a client
# job against the RSS branch feed, which carried all six asset types ten
# apiece. This stays as a second, free check: the item's own link still names
# its type, and trusting one field over a query parameter is one field too
# many for a screen that goes blank if a non-story slips through.
DVIDS_ASSET = re.compile(r'dvidshub\.net/(\w+)/')


def is_story(link):
    """Whether a result is an article rather than a photo or a video.

    A link that names no DVIDS asset type is not a DVIDS item at all -- the
    Army's stories come from army.mil -- so it passes. Only a link that
    positively identifies itself as something else is dropped.
    """
    found = DVIDS_ASSET.search(link or '')
    return found is None or found.group(1) == 'news'


def dvids_leads(json_text, limit=None):
    """api.dvidshub.net's results, projected onto the shape news_row expects.

    Returns [] for anything unparseable or carrying an `errors` body rather
    than raising: a truncated or rejected response must look like "no news",
    not a crash in the same job.
    """
    try:
        data = json.loads(json_text or '')
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict) or 'errors' in data:
        return []
    results = data.get('results')
    if not isinstance(results, list):
        return []

    leads = []
    seen = set()
    cap = limit or DVIDS_LIMIT
    for item in results:
        if len(leads) >= cap:
            break
        if not isinstance(item, dict):
            continue
        # "news:573607" -- the number is the story, and Lead.id is an Int.
        digits = re.sub(r'\D', '', str(item.get('id') or ''))
        if not digits:
            continue
        title = GALLERY_MARKER.sub('', str(item.get('title') or '')).strip()
        # Some results also carry duplicate items sharing one exact title;
        # whichever came first wins, since results are newest first.
        key = ' '.join(title.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        link = item.get('url') or ''
        if not link or not is_story(link):
            continue
        body = str(item.get('short_description') or '')
        when = item.get('date') or item.get('date_published') or ''
        leads.append({
            'id': int(digits),
            'title': title,
            'short_title': title,
            'body': body,
            'url': link,
            'page_url': link,
            'author': item.get('credit') or 'DVIDS',
            'date': as_stored_date(when),
            'last_updated': as_stored_date(when),
            'short_description': body[:200],
            'description': body,
            'section': 'News',
            'category': 'News',
            'keywords': '',
            'image': full_size(item.get('thumbnail')),
        })
    return leads


PRUNE_FLOOR = 0.5
SMALL_FEED = 4


def may_prune(fetched, cached):
    """Whether this feed is complete enough to delete against.

    A short feed and a genuine wave of removals look identical from here
    (corpus_gate.py's argument, one table over), so the ambiguous case does
    nothing: the upsert still runs, only the deletion waits for a feed big
    enough to believe. A first sync has no cache to protect, so it is exempt.
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
    None on any failure, and the caller skips rather than guesses.
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

    The branch clause is the whole point -- without it a Marine Corps run
    deletes every story it did not fetch, including all of the Army's.
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
                         + ', '.join(sorted(DVIDS_BRANCHES)))
    a = ap.parse_args(argv)
    a.branch = canonical_branch(a.branch)

    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not key:
        sys.exit('SUPABASE_SERVICE_KEY not set')

    if a.branch == 'usa':
        # deliberately no browser User-Agent: that is what Akamai rejects
        r = requests.get(LEADS_URL, headers={'accept': 'application/json'}, timeout=120)
        if r.status_code != 200:
            sys.exit(f'army.mil returned {r.status_code} - refusing to touch the '
                     f'cached news rather than replace it with nothing')
        leads = r.json()
    else:
        # every other service comes from DVIDS, same rule on failure
        branch_name = DVIDS_BRANCHES.get(a.branch)
        if not branch_name:
            sys.exit(f'no DVIDS branch known for {a.branch}')
        dvids_key = os.getenv('DVIDS_API_KEY')
        if not dvids_key:
            sys.exit('DVIDS_API_KEY not set - refusing to sync without it')
        r = requests.get(DVIDS_API_URL, params={
            'type': 'news', 'branch': branch_name, 'max_results': DVIDS_LIMIT,
            'api_key': dvids_key,
        }, timeout=120)
        if r.status_code != 200:
            sys.exit(f'dvids returned {r.status_code} - refusing to touch the '
                     f'cached news rather than replace it with nothing')
        leads = dvids_leads(r.text)
        if not leads:
            sys.exit('the API returned nothing usable - refusing to touch the cached news')
        # Most results already carry a thumbnail; only the rest need the
        # article page.
        for lead in leads:
            if lead['image']:
                continue
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

    # Drop stories no longer carried upstream. Scoped to `branch`, same
    # reason as prune_filter. `return=representation` so a prune that
    # removes more than expected leaves a trace instead of a bare count.
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
