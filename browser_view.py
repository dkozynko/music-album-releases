"""Self-contained browser pages for the generated album catalog."""

from html import escape


CSS = """
:root { color-scheme: light dark; --bg: #fff; --fg: #20242a; --muted: #59636e;
  --line: #d8dee5; --stripe: #f5f7fa; --accent: #075bc4;
  --date-width: 92px; --artist-width: 194px; --album-width: 314px; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #171b21; --fg: #e2e7ed; --muted: #a6b1be;
    --line: #38414c; --stripe: #20262e; --accent: #83bcff; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 12px; height: 100vh; height: 100dvh; display: flex;
  flex-direction: column; gap: 10px; background: var(--bg); color: var(--fg);
  font: 13px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { flex: none; }
h1 { margin: 0 0 6px; font-size: 20px; font-weight: 650; }
nav { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; }
a { color: var(--accent); text-underline-offset: 2px; }
p { margin: 6px 0 0; color: var(--muted); }
.table-scroll { flex: 1; min-height: 0; width: 100%; overflow: auto;
  border: 1px solid var(--line); border-radius: 4px; }
table { border-collapse: separate; border-spacing: 0; min-width: 100%; font-size: 13px; }
th, td { padding: 3px 7px; text-align: left; white-space: nowrap;
  border-bottom: 1px solid var(--line); vertical-align: top; }
thead th { position: sticky; top: 0; z-index: 2; background: var(--bg);
  box-shadow: 0 1px 0 var(--line); }
thead th:nth-child(-n+4), tbody td:nth-child(-n+4) { position: sticky;
  background: var(--bg); z-index: 1; }
thead th:nth-child(-n+4) { z-index: 3; }
thead th:nth-child(1), tbody td:nth-child(1) { left: 0; min-width: var(--date-width); }
thead th:nth-child(2), tbody td:nth-child(2) { left: var(--date-width); min-width: var(--date-width); }
thead th:nth-child(3), tbody td:nth-child(3) { left: calc(2 * var(--date-width)); }
thead th:nth-child(4), tbody td:nth-child(4) { left: calc(2 * var(--date-width) + var(--artist-width)); }
.identity { display: block; overflow: hidden; text-overflow: ellipsis; }
td:nth-child(3) .identity { width: calc(var(--artist-width) - 14px); }
td:nth-child(4) .identity { width: calc(var(--album-width) - 14px); }
tbody tr:nth-child(even) td { background: var(--stripe); }
tbody tr:hover td { background: var(--line); }
.month th { padding-top: 10px; padding-bottom: 6px; background: var(--stripe); }
td:nth-child(-n+2) { font-variant-numeric: tabular-nums; }
@media (max-width: 900px) {
  tbody td:nth-child(-n+4) { position: static; }
  thead th:nth-child(-n+4) { left: auto; }
}
@media (max-width: 600px) { body { padding: 8px; } h1 { font-size: 17px; } }
""".strip()


def render(year, month_rows, headings, years):
    navigation = [f'<strong aria-current="page">{value}</strong>' if value == year else
                  f'<a href="{value:04d}.html">{value}</a>' for value in sorted(set(years) | {year}, reverse=True)]
    count = sum(len(rows) for _, rows in month_rows)
    lines = ['<!doctype html>', '<html lang="en">', '<head>', '<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<title>Post-rock / post-metal albums — {year}</title>', '<style>', CSS, '</style>', '</head>', '<body>',
             '<header>', f'<h1>Post-rock / post-metal albums — {year}</h1>',
             '<nav aria-label="Year and downloads">' + ' '.join(navigation) +
             f' <span>{count:,} albums</span> <a href="{year:04d}.csv" download>Download CSV</a>' +
             f' <a href="{year:04d}.md">Markdown</a></nav>',
             '<p>Added = first recorded date in this catalog (UTC). Last.fm tags describe the artist. '
             'Scroll horizontally for full tags, sources and notes.</p>', '</header>',
             '<main class="table-scroll" tabindex="0" aria-label="Album releases">', '<table>',
             '<thead><tr>' + ''.join(f'<th scope="col">{escape(label)}</th>' for label in headings) + '</tr></thead>']
    for month, rows in month_rows:
        lines.append('<tbody>')
        lines.append(f'<tr class="month"><th colspan="{len(headings)}" scope="rowgroup">{escape(month)}</th></tr>')
        lines.extend(rows)
        lines.append('</tbody>')
    lines += ['</table>', '</main>', '</body>', '</html>', '']
    return '\n'.join(lines)


def index(years):
    latest = max(years)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url={latest:04d}.html">'
            '<title>Album releases</title></head><body>'
            f'<a href="{latest:04d}.html">Open the {latest} album calendar</a></body></html>\n')
