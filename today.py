import os
import json
import time
import datetime
import requests

USER_NAME = os.getenv('USER_NAME', 'Unkn-0-wngb')
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')
HEADERS = {'authorization': f'token {ACCESS_TOKEN}'}

QUERY_COUNT = {
    'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0,
    'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0
}

BASE_DIR = os.path.dirname(__file__)
CACHE_DIR = os.path.join(BASE_DIR, 'cache')
CACHE_FILE = os.path.join(CACHE_DIR, f'{USER_NAME.lower()}.csv')
GRID_FILE = os.path.join(BASE_DIR, 'assets', 'face_grid.json')


def bump(fn_name):
    QUERY_COUNT[fn_name] += 1


def timed(fn, *args):
    t0 = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - t0


def log_time(label, secs):
    print('{:<23}'.format('   ' + label + ':'), end='')
    if secs > 1:
        print(f'{secs:.4f} s')
    else:
        print(f'{secs * 1000:.4f} ms')


def gql(fn_name, query, variables):
    resp = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS
    )
    if resp.status_code != 200:
        raise Exception(fn_name, 'failed with', resp.status_code, resp.text, QUERY_COUNT)
    return resp


def user_getter(username):
    bump('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    data = gql('user_getter', query, {'login': username}).json()['data']['user']
    return {'id': data['id']}, data['createdAt']


def follower_getter(username):
    bump('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    resp = gql('follower_getter', query, {'login': username})
    return int(resp.json()['data']['user']['followers']['totalCount'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    bump('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        stargazers {
                            totalCount
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    repos = gql('graph_repos_stars', query, variables).json()['data']['user']['repositories']

    if count_type == 'repos':
        return repos['totalCount']
    elif count_type == 'stars':
        return sum(edge['node']['stargazers']['totalCount'] for edge in repos['edges'])
    return 0


def graph_commits(start_date, end_date):
    bump('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    resp = gql('graph_commits', query, variables)
    return int(resp.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def loc_query(owner_affiliation, force_cache=False, cursor=None, edges=None):
    edges = edges or []
    bump('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    repos = gql('loc_query', query, variables).json()['data']['user']['repositories']
    edges += repos['edges']

    if repos['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, force_cache, repos['pageInfo']['endCursor'], edges)
    return cache_builder(edges, force_cache)


def cache_builder(edges, force_cache, loc_add=0, loc_del=0):
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'w') as f:
            for edge in edges:
                f.write(f"{edge['node']['nameWithOwner']} 0 0 0 0\n")

    with open(CACHE_FILE) as f:
        lines = f.readlines()

    lookup = {ln.split()[0]: i for i, ln in enumerate(lines)}

    for edge in edges:
        repo = edge['node']['nameWithOwner']
        ref = edge['node']['defaultBranchRef']
        commits_now = ref['target']['history']['totalCount'] if ref else 0

        if repo not in lookup:
            lines.append(f'{repo} 0 0 0 0\n')
            lookup[repo] = len(lines) - 1

        i = lookup[repo]
        commits_cached = int(lines[i].split()[1])

        if commits_now != commits_cached or force_cache:
            owner, name = repo.split('/', 1)
            adds, dels, mine = recursive_loc(owner, name)
            lines[i] = f'{repo} {commits_now} {mine} {adds} {dels}\n'

    with open(CACHE_FILE, 'w') as f:
        f.writelines(lines)

    for ln in lines:
        _, _, _, adds, dels = ln.split()
        loc_add += int(adds)
        loc_del += int(dels)

    return [loc_add, loc_del, loc_add - loc_del]


def recursive_loc(owner, repo_name, add_total=0, del_total=0, my_commits=0, cursor=None):
    bump('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                        author {
                                            user {
                                                id
                                            }
                                        }
                                        deletions
                                        additions
                                    }
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}

    try:
        resp = gql('recursive_loc', query, variables)
    except Exception:
        return 0, 0, 0

    repo_data = resp.json()['data']['repository']
    if not repo_data or not repo_data['defaultBranchRef']:
        return 0, 0, 0

    history = repo_data['defaultBranchRef']['target']['history']
    for node in history['edges']:
        commit = node['node']
        author_id = ((commit.get('author') or {}).get('user') or {}).get('id')
        if author_id == OWNER_ID:
            my_commits += 1
            add_total += commit['additions']
            del_total += commit['deletions']

    if history['pageInfo']['hasNextPage']:
        return recursive_loc(owner, repo_name, add_total, del_total, my_commits,
                              history['pageInfo']['endCursor'])
    return add_total, del_total, my_commits


def age_string(created_at):
    created = datetime.datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')
    now = datetime.datetime.utcnow()
    yrs = now.year - created.year - ((now.month, now.day) < (created.month, created.day))
    return f"{yrs} yr{'s' if yrs != 1 else ''} on GitHub"


NAVY_DARK = '#000000'
PANEL_LINE = '#2a2a2a'
TERRACOTTA = '#e8376a'
TERRACOTTA_BRIGHT = '#f16a91'
CREAM = '#ffffff'
MUTED = '#999999'
GREEN = '#8fbf7f'
RED = '#d97070'
RIM = '#ffb98f'

SHADOW = (70, 38, 28)
HIGHLIGHT = (235, 190, 150)


def lerp_color(t):
    r = int(SHADOW[0] + (HIGHLIGHT[0] - SHADOW[0]) * t)
    g = int(SHADOW[1] + (HIGHLIGHT[1] - SHADOW[1]) * t)
    b = int(SHADOW[2] + (HIGHLIGHT[2] - SHADOW[2]) * t)
    return f'#{r:02x}{g:02x}{b:02x}'


N_BUCKETS = 32
PALETTE = [lerp_color(i / (N_BUCKETS - 1)) for i in range(N_BUCKETS)]


def bucket_for(p):
    t = (255 - p) / 255
    return min(N_BUCKETS - 1, max(0, int(t * N_BUCKETS)))


FONT_SIZE_ART = 6.5
LINE_H_ART = 7.0
CHAR_W_ART = FONT_SIZE_ART * 0.6


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def kv(panel_x, label, value, y, value_class='value'):
    return (f'<text x="{panel_x + 12}" y="{y}" class="key">{esc(label)}</text>'
            f'<text x="{panel_x + 190}" y="{y}" class="{value_class}">{esc(value)}</text>')


def divider(panel_x, label, y, width):
    return (f'<text x="{panel_x}" y="{y}" class="section">{esc(label.upper())}</text>'
            f'<line x1="{panel_x}" y1="{y+8}" x2="{panel_x + width}" y2="{y+8}" '
            f'stroke="{PANEL_LINE}" stroke-width="1"/>')


def build_card(stats):
    with open(GRID_FILE) as f:
        grid = json.load(f)
    chars, lum, kind = grid['chars'], grid['lum'], grid['kind']
    cols, rows = grid['cols'], grid['rows']

    art_width = cols * CHAR_W_ART
    art_height = rows * LINE_H_ART

    margin = 34
    art_x = margin
    panel_x = art_x + art_width + 56
    card_w = panel_x + 560
    content_w = 500

    rows_svg = []
    y = 56
    rows_svg.append(f'<circle cx="{panel_x + 5}" cy="{y-5}" r="4" fill="{TERRACOTTA_BRIGHT}"/>')
    rows_svg.append(f'<text x="{panel_x + 18}" y="{y}" class="header">joshua@laveryjonez</text>')
    y += 14
    rows_svg.append(f'<text x="{panel_x + 18}" y="{y}" class="tagline">Developer &amp; Fabricator — Argyll &amp; Bute, Scotland</text>')
    y += 14
    rows_svg.append(f'<line x1="{panel_x}" y1="{y}" x2="{panel_x + 500}" y2="{y}" stroke="{TERRACOTTA}" stroke-opacity="0.45" stroke-width="1.4"/>')
    y += 30

    fields_top = [
        ('OS', 'EndeavourOS, iOS'),
        ('Uptime', stats['age']),
        ('Host', 'Gigabyte H81M-DS2V (i7-4770)'),
        ('Kernel', 'ACF — Corporal (4 yrs service)'),
        ('IDE', 'VS Code'),
        ('School', 'Campbeltown Grammar School & UHI Argyll'),
    ]
    for label, value in fields_top:
        rows_svg.append(kv(panel_x, label, value, y))
        y += 21

    y += 14
    rows_svg.append(divider(panel_x, 'Languages', y, content_w)); y += 26
    rows_svg.append(kv(panel_x, 'Programming', 'Python, JavaScript, TypeScript, Java,', y)); y += 22
    rows_svg.append(f'<text x="{panel_x + 190}" y="{y}" class="value">PowerShell, Bash</text>'); y += 21
    rows_svg.append(kv(panel_x, 'Computer', 'HTML5, CSS3, SQL', y)); y += 21
    rows_svg.append(kv(panel_x, 'Spoken', 'English (native)', y)); y += 21
    rows_svg.append(f'<text x="{panel_x + 190}" y="{y}" class="value">Welsh, Russian, French, Spanish (basic)</text>'); y += 21

    y += 14
    rows_svg.append(divider(panel_x, 'Hobbies', y, content_w)); y += 26
    rows_svg.append(kv(panel_x, 'Software', 'Brand & web design, self-hosted mail server', y)); y += 21
    rows_svg.append(kv(panel_x, 'Practical', 'PC building, electrical wiring, fabrication', y)); y += 21
    rows_svg.append(kv(panel_x, 'Security', 'Kali Linux, network scanning, OSINT', y)); y += 21

    y += 14
    rows_svg.append(divider(panel_x, 'Contact', y, content_w)); y += 26
    rows_svg.append(
        f'<text x="{panel_x + 12}" y="{y}" class="key">Email</text>'
        f'<a href="mailto:joshua@laveryjonez.uk">'
        f'<text x="{panel_x + 190}" y="{y}" class="value link">joshua@laveryjonez.uk</text></a>'
    ); y += 21
    rows_svg.append(
        f'<text x="{panel_x + 12}" y="{y}" class="key">Website</text>'
        f'<a href="https://laveryjonez.uk" target="_blank" rel="noopener">'
        f'<text x="{panel_x + 190}" y="{y}" class="value link">laveryjonez.uk</text></a>'
    ); y += 21

    y += 14
    rows_svg.append(divider(panel_x, 'Socials', y, content_w)); y += 30

    social_groups = [
        ('lavery.jonez', [
            ('Instagram', 'https://www.instagram.com/lavery.jonez/'),
            ('TikTok', 'https://www.tiktok.com/@lavery.jonez'),
        ]),
        ('laveryjonez', [
            ('YouTube', 'https://www.youtube.com/@LaveryJonez'),
            ('Twitch', 'https://www.twitch.tv/laveryjonez'),
            ('X', 'https://x.com/laveryjonez'),
        ]),
        ('Unkn-0-wngb', [
            ('GitHub', 'https://github.com/Unkn-0-wngb'),
            ('Steam', 'https://steamcommunity.com/profiles/76561199067544848'),
        ]),
        ('Joshua Lavery-Jones', [
            ('LinkedIn', 'https://uk.linkedin.com/in/joshua-lavery-jones-8b4662333'),
            ('Facebook', 'https://www.facebook.com/profile.php?id=61566072800171'),
        ]),
    ]

    col_positions = [panel_x + 12, panel_x + 270]
    group_start_y = y
    row_h = 19
    col_top_gap = 18

    for i, (handle, platforms) in enumerate(social_groups):
        col = i % 2
        row_pair = i // 2
        gx = col_positions[col]
        gy = group_start_y + row_pair * (col_top_gap + row_h * 3 + 8)
        rows_svg.append(f'<text x="{gx}" y="{gy}" class="handle">{esc(handle)}</text>')
        for j, (platform, url) in enumerate(platforms):
            py = gy + col_top_gap + j * row_h
            rows_svg.append(
                f'<a href="{esc(url)}" target="_blank" rel="noopener">'
                f'<text x="{gx + 16}" y="{py}" class="key link">{esc(platform)}</text>'
                f'</a>'
            )

    n_rows_of_groups = (len(social_groups) + 1) // 2
    y = group_start_y + n_rows_of_groups * (col_top_gap + row_h * 3 + 8) - 8

    y += 14
    rows_svg.append(divider(panel_x, 'GitHub Stats', y, content_w)); y += 26
    repos_str = str(stats['repos'])
    x_repos = panel_x + 90
    x_contrib = x_repos + len(repos_str) * 7.3 + 6
    rows_svg.append(
        f'<text x="{panel_x + 12}" y="{y}" class="key">Repos</text>'
        f'<text x="{x_repos:.0f}" y="{y}" class="statnum">{esc(repos_str)}</text>'
        f'<text x="{x_contrib:.0f}" y="{y}" class="value" fill="{MUTED}">{{Contributed: {stats["contributed"]}}}</text>'
        f'<text x="{panel_x + 300}" y="{y}" class="key">Stars</text>'
        f'<text x="{panel_x + 350}" y="{y}" class="statnum">{stats["stars"]}</text>'
    )
    y += 21
    rows_svg.append(
        f'<text x="{panel_x + 12}" y="{y}" class="key">Commits</text>'
        f'<text x="{panel_x + 90}" y="{y}" class="statnum">{stats["commits_this_year"]}</text>'
        f'<text x="{panel_x + 300}" y="{y}" class="key">Followers</text>'
        f'<text x="{panel_x + 380}" y="{y}" class="statnum">{stats["followers"]}</text>'
    )
    y += 21
    loc_add_str = f'{stats["loc_add"]:,}'
    loc_del_str = f'{stats["loc_del"]:,}'
    char_w = 7.3
    x_loc_add = panel_x + 130
    x_add_sign = x_loc_add + len(loc_add_str) * char_w + 4
    x_comma = x_add_sign + 20
    x_loc_del = x_comma + 12
    x_del_sign = x_loc_del + len(loc_del_str) * char_w + 4
    rows_svg.append(
        f'<text x="{panel_x + 12}" y="{y}" class="key">Lines of Code</text>'
        f'<text x="{x_loc_add:.0f}" y="{y}" class="statnum">{esc(loc_add_str)}</text>'
        f'<text x="{x_add_sign:.0f}" y="{y}" class="add">++</text>'
        f'<text x="{x_comma:.0f}" y="{y}" class="value">,</text>'
        f'<text x="{x_loc_del:.0f}" y="{y}" class="statnum">{esc(loc_del_str)}</text>'
        f'<text x="{x_del_sign:.0f}" y="{y}" class="del">--</text>'
    )
    y += 34

    rows_svg.append(f'<text x="{panel_x}" y="{y}" class="footer">generated automatically · refreshed daily</text>')
    y += margin

    stats_block = '\n'.join(rows_svg)
    card_h = max(y, art_height + margin * 2)

    art_y = (card_h - art_height) / 2 + LINE_H_ART
    art_tspans = []
    for r in range(rows):
        ay = art_y + r * LINE_H_ART
        row_chars = chars[r]
        row_lum = lum[r]
        row_kind = kind[r]

        def color_for(c):
            if row_kind[c] == 2:
                return RIM
            return PALETTE[bucket_for(row_lum[c])]

        segments = []
        cur_color = None
        cur_start = 0
        for c in range(cols):
            is_blank = row_chars[c] == ' '
            col = None if is_blank else color_for(c)
            if col != cur_color:
                if cur_color is not None:
                    segments.append((cur_start, c, cur_color))
                cur_color = col
                cur_start = c
        if cur_color is not None:
            segments.append((cur_start, cols, cur_color))
        if not segments:
            continue

        tspan_parts = []
        for (start, end, color) in segments:
            text = ''.join(row_chars[start:end])
            tx = art_x + start * CHAR_W_ART
            tspan_parts.append(f'<tspan x="{tx:.1f}" fill="{color}">{esc(text)}</tspan>')
        art_tspans.append(f'<text y="{ay:.1f}" class="art" xml:space="preserve">{"".join(tspan_parts)}</text>')

    art_block = '\n'.join(art_tspans)
    divider_x = art_x + art_width + 28

    glow_pad = 28
    outer_w = card_w + glow_pad * 2
    outer_h = card_h + glow_pad * 2

    return f'''<svg width="{outer_w:.0f}" height="{outer_h:.0f}" viewBox="0 0 {outer_w:.0f} {outer_h:.0f}" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
<defs>
<radialGradient id="glow" cx="50%" cy="50%" r="50%">
<stop offset="0%" stop-color="{TERRACOTTA}" stop-opacity="0.12"/>
<stop offset="100%" stop-color="{TERRACOTTA}" stop-opacity="0"/>
</radialGradient>
<linearGradient id="topGlow" x1="0" y1="0" x2="0" y2="1">
<stop offset="0%" stop-color="{TERRACOTTA}" stop-opacity="0.06"/>
<stop offset="100%" stop-color="{TERRACOTTA}" stop-opacity="0"/>
</linearGradient>
<linearGradient id="topBar" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="{TERRACOTTA}" stop-opacity="0"/>
<stop offset="50%" stop-color="{TERRACOTTA}" stop-opacity="0.9"/>
<stop offset="100%" stop-color="{TERRACOTTA}" stop-opacity="0"/>
</linearGradient>
<filter id="outerGlow" x="-50%" y="-50%" width="200%" height="200%">
<feGaussianBlur stdDeviation="10" result="blur"/>
<feColorMatrix in="blur" mode="matrix" values="0 0 0 0 0.91  0 0 0 0 0.21  0 0 0 0 0.41  0 0 0 0.5 0"/>
</filter>
</defs>
<style>
.link {{ text-decoration: none; }}
.link:hover {{ text-decoration: underline; }}
.bg {{ fill: {NAVY_DARK}; }}
.border {{ fill: none; stroke: {TERRACOTTA}; stroke-width: 1.5; }}
.art {{ font: 700 {FONT_SIZE_ART}px 'Consolas', 'Courier New', monospace; letter-spacing: -0.2px; }}
.header {{ font: 700 17px 'Poppins', 'Consolas', monospace; fill: {CREAM}; }}
.tagline {{ font: 400 10.5px 'Consolas', monospace; fill: {MUTED}; letter-spacing: 0.02em; }}
.section {{ font: 700 10.5px 'Consolas', monospace; fill: {TERRACOTTA_BRIGHT}; letter-spacing: 0.14em; }}
.handle {{ font: 700 12.5px 'Consolas', monospace; fill: {CREAM}; }}
.key {{ font: 500 12px 'Consolas', monospace; fill: {TERRACOTTA}; }}
.value {{ font: 400 12px 'Consolas', monospace; fill: {CREAM}; }}
.statnum {{ font: 700 12.5px 'Consolas', monospace; fill: {CREAM}; }}
.add {{ font: 700 12px 'Consolas', monospace; fill: {GREEN}; }}
.del {{ font: 700 12px 'Consolas', monospace; fill: {RED}; }}
.footer {{ font: 400 10px 'Consolas', monospace; fill: {MUTED}; opacity: 0.7; }}
</style>
<g transform="translate({glow_pad},{glow_pad})">
<rect class="bg" width="{card_w:.0f}" height="{card_h:.0f}" rx="14" filter="url(#outerGlow)" opacity="0.6"/>
<rect class="bg" width="{card_w:.0f}" height="{card_h:.0f}" rx="14"/>
<rect width="{card_w:.0f}" height="120" fill="url(#topGlow)"/>
<rect x="14" y="0" width="{card_w-28:.0f}" height="2" fill="url(#topBar)"/>
<ellipse cx="{art_x + art_width/2:.0f}" cy="{art_y + art_height*0.3:.0f}" rx="{art_width*0.8:.0f}" ry="{art_height*0.55:.0f}" fill="url(#glow)"/>
{art_block}
<line x1="{divider_x:.0f}" y1="{margin:.0f}" x2="{divider_x:.0f}" y2="{card_h-margin:.0f}" stroke="{PANEL_LINE}" stroke-width="1"/>
{stats_block}
<rect class="border" x="0.75" y="0.75" width="{card_w-1.5:.0f}" height="{card_h-1.5:.0f}" rx="13"/>
</g>
</svg>'''


if __name__ == '__main__':
    if not ACCESS_TOKEN:
        raise SystemExit('need ACCESS_TOKEN set (personal access token, repo + read:user scopes)')

    print(f'pulling stats for {USER_NAME}...\n')

    (owner, acc_date), t = timed(user_getter, USER_NAME)
    OWNER_ID = owner['id']
    log_time('account data', t)

    followers, t = timed(follower_getter, USER_NAME)
    log_time('followers', t)

    total_repos, t = timed(graph_repos_stars, 'repos', ['OWNER'])
    log_time('repos', t)

    contributed_repos, t = timed(graph_repos_stars, 'repos',
                                  ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    log_time('contributed repos', t)

    stars, t = timed(graph_repos_stars, 'stars', ['OWNER'])
    log_time('stars', t)

    yr = datetime.date.today().year
    commits_this_year, t = timed(graph_commits, f'{yr}-01-01T00:00:00Z', f'{yr}-12-31T23:59:59Z')
    log_time('commits this year', t)

    loc, t = timed(loc_query, ['OWNER'])
    log_time('lines of code', t)

    stats = {
        'age': age_string(acc_date),
        'repos': total_repos,
        'contributed': contributed_repos,
        'stars': stars,
        'followers': followers,
        'commits_this_year': commits_this_year,
        'loc_add': loc[0],
        'loc_del': loc[1],
        'loc_net': loc[2],
    }

    card = build_card(stats)
    with open('dark_mode.svg', 'w') as f:
        f.write(card)
    with open('light_mode.svg', 'w') as f:
        f.write(card)

    print(f'\ndone — {sum(QUERY_COUNT.values())} api calls, cards written')
