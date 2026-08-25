"""
Cache army.mil news into Supabase.

api.army.mil sits behind Akamai, which returns 403 to any request carrying a
browser User-Agent and separately rejects Cloud Run's egress range regardless
of User-Agent. So the wasm client cannot call it and neither can our /proxy.
This job fetches it from a GitHub Actions runner and writes the result to a
table the client already has access to.

    SUPABASE_SERVICE_KEY=... python sync_news.py            # dry run
    SUPABASE_SERVICE_KEY=... python sync_news.py --apply
"""
import argparse
import os
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


def news_row(lead):
    """Project one API lead onto our columns, or None if it cannot satisfy the
    client's model.

    Writing a row that is missing a non-nullable field would make
    kotlinx.serialization throw for the WHOLE list, so one malformed lead would
    blank the entire news screen. Skipping it costs one story instead.
    """
    if any(lead.get(k) is None for k in REQUIRED):
        return None
    row = {k: lead[k] for k in REQUIRED}
    for k in OPTIONAL:
        row[k] = lead.get(k)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not key:
        sys.exit('SUPABASE_SERVICE_KEY not set')

    # Deliberately no browser User-Agent: that is what Akamai rejects.
    r = requests.get(LEADS_URL, headers={'accept': 'application/json'}, timeout=120)
    if r.status_code != 200:
        sys.exit(f'army.mil returned {r.status_code} - refusing to touch the '
                 f'cached news rather than replace it with nothing')
    leads = r.json()
    rows = [row for row in (news_row(x) for x in leads) if row]
    skipped = len(leads) - len(rows)
    print(f'fetched {len(leads)} leads, {len(rows)} usable'
          + (f', {skipped} skipped for missing fields' if skipped else ''))

    if not rows:
        sys.exit('no usable leads - leaving the existing cache alone')
    if not a.apply:
        print('Dry run. Re-run with --apply.')
        for row in rows[:5]:
            print(f'  would upsert {row["id"]}  {row["title"][:60]}')
        return 0

    h = {'apikey': key, 'Authorization': f'Bearer {key}',
         'Content-Type': 'application/json',
         'Prefer': 'resolution=merge-duplicates,return=minimal'}
    resp = requests.post(f'{SUPABASE_URL}/rest/v1/news', headers=h, json=rows, timeout=120)
    if resp.status_code >= 300:
        sys.exit(f'upsert failed {resp.status_code}: {resp.text[:300]}')

    # Drop stories no longer carried upstream, so the screen matches army.mil.
    keep = ','.join(str(row['id']) for row in rows)
    d = requests.delete(f'{SUPABASE_URL}/rest/v1/news',
                        headers={k: v for k, v in h.items() if k != 'Prefer'},
                        params={'id': f'not.in.({keep})'}, timeout=120)
    if d.status_code >= 300:
        print(f'  WARN prune failed {d.status_code}: {d.text[:160]}')
    print(f'DONE. cached {len(rows)} stories')
    return 0


if __name__ == '__main__':
    sys.exit(main())
