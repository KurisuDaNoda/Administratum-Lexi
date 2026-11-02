"""Snapshot and extract Wahapedia numbered rules; no database activation."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

URL = 'https://wahapedia.ru/wh40k11ed/the-rules/core-rules/'


def extract(html, url=URL):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.find(id='wahMainContent')
    if root is None:
        raise ValueError('Expected rules content missing; page structure may have changed')
    for node in root.select('script, style, noscript, form, .noprint, [id^="tooltip_content"], [class*="page_ads"]'):
        node.decompose()
    # Dice are CSS drawings, not text or images. Preserve their numerical values.
    for node in root.find_all(class_=re.compile(r'^cruD[1-6]$')):
        value = next(c[-1] for c in node.get('class', []) if re.fullmatch(r'cruD[1-6]', c))
        node.replace_with(' ' + value + ' ')
    headings = {}
    for number in root.select('.h_number'):
        heading = number.parent
        if id(heading) in headings:
            continue
        ref = number.get_text(strip=True)
        anchor = heading.get('id')
        if not anchor and 'abName' in heading.get('class', []):
            # These cards have no individual HTML anchor on this page.
            anchor = 'Core-Abilities'
        if not anchor or not re.fullmatch(r'\d{2}\.\d{2}(?:\.\d{2})*', ref):
            continue
        headings[id(heading)] = (ref, heading.get_text(' ', strip=True).removesuffix(ref).strip(), anchor)
    sections = []
    current = None
    parts = []

    def finish():
        if current is not None:
            text = ''.join(parts)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r' *\n *', '\n', text)
            current['body'] = re.sub(r'\n{3,}', '\n\n', text).strip()
            current['body'] = re.sub(r'\b([1-6]) \+', r'\1+', current['body'])
            sections.append(current.copy())

    def walk(node):
        nonlocal current, parts
        if isinstance(node, NavigableString):
            if current is not None:
                parts.append(str(node).replace('\r', '').replace('\n', ' '))
            return
        if not isinstance(node, Tag):
            return
        if id(node) in headings:
            finish()
            ref, title, anchor = headings[id(node)]
            aliases = [n.get_text(strip=True) for n in node.select('.h_number')]
            current = {'rule_number': ref, 'additional_rule_numbers': aliases[1:],
                       'title': title, 'source_url': url + '#' + anchor, 'links': []}
            parts = []
            return
        if node.name == 'a' and node.get('href') and current is not None:
            link = urljoin(url, node['href'])
            if link not in current['links']:
                current['links'].append(link)
        if node.name == 'img' and current is not None:
            src = urljoin(url, node.get('src', ''))
            if node.get('alt'):
                parts.append(' ' + node['alt'] + ' ')
            elif 'ArrowRight' not in src:
                parts.append(' [Illustration: ' + src + '] ')
        block = node.name in {'p', 'div', 'ul', 'ol', 'li', 'table', 'tr', 'h2', 'h3', 'h4'}
        if block or node.name == 'br':
            parts.append('\n')
        if node.name == 'li':
            parts.append('- ')
        for child in node.children:
            walk(child)
        if node.name in {'td', 'th'}:
            parts.append(' | ')
        if block:
            parts.append('\n')

    walk(root)
    finish()
    if not sections:
        raise ValueError('No numbered rules found')
    return sections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=URL)
    parser.add_argument('--html', type=Path, help='Parse an existing UTF-8 snapshot without fetching')
    parser.add_argument('--output', type=Path, default=Path('generated/wahapedia'))
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in {'wahapedia.ru', 'www.wahapedia.ru'}:
        parser.error('Expected a Wahapedia source URL')
    if args.html:
        raw = args.html.read_bytes()
        retrieved = datetime.fromtimestamp(args.html.stat().st_mtime, timezone.utc).isoformat()
    else:
        request = urllib.request.Request(args.url, headers={'User-Agent': 'LexiRulesImporter/0.1'})
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
        retrieved = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(raw).hexdigest()
    output = args.output / digest
    output.mkdir(parents=True, exist_ok=True)
    sections = extract(raw.decode('utf-8'), args.url)
    result = {'source_url': args.url, 'edition': 11 if '/wh40k11ed/' in args.url else None,
              'source_kind': 'secondary_compilation', 'retrieved_at': retrieved,
              'published_at': None, 'sha256': digest, 'extractor': 'wahapedia-numbered-v1',
              'status': 'extracted_for_review', 'sections': sections}
    (output / 'source.html').write_bytes(raw)
    (output / 'rules.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'rules.md').write_text('\n\n'.join(
        f"## {s['rule_number']} {s['title']}\n\nSource: {s['source_url']}\n\n{s['body']}" for s in sections), encoding='utf-8')
    print(json.dumps({'output': str(output), 'rules': len(sections), 'status': result['status']}))


if __name__ == '__main__':
    main()
