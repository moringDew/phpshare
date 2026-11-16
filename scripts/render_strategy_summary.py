"""Render the strategy memo's Markdown subset as standalone, offline HTML.

Run: python scripts/render_strategy_summary.py [docs/md/report.md] or --all
Supports project headings, tables, lists, quotes, links, fenced code and emphasis.
"""
from html import escape, unescape
from pathlib import Path
import re
import argparse
import os
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
MD_ROOT = ROOT / "docs" / "md"
HTML_ROOT = ROOT / "docs" / "html"
SOURCE = MD_ROOT / "策略汇总.md"


def inline(text):
    parts = re.split(r"(`[^`]+`|!?\[[^\]]+\]\([^\s)]+\))", text)
    result = []
    for part in parts:
        if part.startswith("`") and part.endswith("`"):
            result.append("<code>" + escape(part[1:-1]) + "</code>")
        elif re.fullmatch(r'!?\[[^\]]+\]\([^\s)]+\)', part):
            match = re.fullmatch(r'(!?)\[([^\]]+)\]\(([^\s)]+)\)', part)
            image, label, target = match.groups()
            url = urlsplit(target)
            if url.scheme.lower() not in ('', 'http', 'https', 'mailto'):
                result.append(escape(label))
                continue
            target = escape(quote(target, safe='/:#?=&%+@'))
            if image:
                result.append(f'<img src="{target}" alt="{escape(label)}">')
            else:
                result.append(f'<a href="{target}">{inline(label)}</a>')
        else:
            result.append(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(part)))
    return "".join(result)


def render(lines, toc):
    out = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        heading = re.match(r"^(#{1,6}) (.+)$", line)
        item = re.match(r"^( *)(-|\d+\.) (.*)$", lines[i])
        if line.startswith('```'):
            language = line[3:].strip()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code.append(lines[i])
                i += 1
            if i == len(lines):
                raise ValueError('Unclosed fenced code block')
            i += 1
            out.append(f'<pre><code class="language-{escape(language)}">' +
                       escape('\n'.join(code)) + '</code></pre>')
        elif heading:
            level, title = len(heading[1]), heading[2]
            anchor = f"section-{len(toc)}"
            toc.append((level, title, anchor))
            out.append(f'<h{level} id="{anchor}">{inline(title)}</h{level}>')
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            aligns = ["right" if c.endswith(":") else "left" for c in rows[1]]
            is_wide = len(rows[0]) > 8
            table_class = ' class="wide"' if is_wide else ''
            if is_wide:
                out.append('<div class="table-hint">表格较宽，可左右滑动查看全部列。</div>')
            out.append(f'<div class="table-wrap"><table{table_class}><thead><tr>')
            out.extend(f'<th>{inline(c)}</th>' for c in rows[0])
            out.append('</tr></thead><tbody>')
            for row in rows[2:]:
                out.append('<tr>' + ''.join(
                    f'<td style="text-align:{aligns[n]}">{inline(c)}</td>'
                    for n, c in enumerate(row)) + '</tr>')
            out.append('</tbody></table></div>')
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(inline(lines[i].strip()[1:].strip()))
                i += 1
            out.append('<blockquote>' + '<br>'.join(quote) + '</blockquote>')
        elif item:
            indent = len(item[1])
            tag = 'ul' if item[2] == '-' else 'ol'
            out.append(f'<{tag}>')
            while i < len(lines):
                match = re.match(r"^( *)(-|\d+\.) (.*)$", lines[i])
                if not match or len(match[1]) != indent:
                    break
                body = [match[3]]
                i += 1
                while i < len(lines):
                    if not lines[i].strip():
                        i += 1
                        continue
                    leading = len(lines[i]) - len(lines[i].lstrip())
                    if leading <= indent:
                        break
                    body.append(lines[i][indent + 2:])
                    i += 1
                content = render(body, toc)
                content = re.sub(r'^<p>\[([ xX])\] ', lambda m:
                    '<p><input type="checkbox" disabled' +
                    (' checked' if m[1].lower() == 'x' else '') + '> ', content)
                out.append('<li>' + content + '</li>')
            out.append(f'</{tag}>')
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^\s*(```|#|\||>|- |\d+\. )", lines[i]):
                paragraph.append(lines[i].strip())
                i += 1
            out.append('<p>' + inline(' '.join(paragraph)) + '</p>')
    return '\n'.join(out)


def render_document(source):
    source = source.resolve()
    output = html_path(source)
    toc = []
    body = render(source.read_text(encoding='utf-8-sig').splitlines(), toc)
    def relocate_link(match):
        url = urlsplit(unescape(match[2]))
        if url.scheme or url.netloc or not url.path:
            return match[0]
        target = (source.parent / unquote(url.path)).resolve()
        if target.suffix == '.md' and MD_ROOT in target.parents:
            target = html_path(target)
        path = os.path.relpath(target, output.parent).replace(os.sep, '/')
        href = quote(urlunsplit(url._replace(path=path)), safe='/:#?=&%+@')
        return f'{match[1]}="{escape(href)}"'
    body = re.sub(r'(href|src)="([^"]*)"', relocate_link, body)
    title = next((title for level, title, _ in toc if level == 1), source.stem)
    nav = ''.join(f'<a href="#{anchor}">{escape(title)}</a>'
                  for level, title, anchor in toc if level == 2)
    page = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__DOCUMENT_TITLE__</title>
<style>
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f5f8;color:#243247;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}
.layout{max-width:1420px;margin:auto;display:grid;grid-template-columns:230px minmax(0,1fr);gap:32px;padding:40px 28px}
nav{position:sticky;top:32px;align-self:start;font-size:14px}nav strong{display:block;margin-bottom:16px}nav a{display:block;padding:7px 0;color:#52627a;text-decoration:none}a:hover{color:#1768ad}
main{min-width:0;padding:36px 44px;background:white;border:1px solid #e1e6ee;border-radius:12px}h1{font-size:32px;margin:0 0 24px}h2{font-size:23px;margin:44px 0 18px;border-bottom:1px solid #e1e6ee;padding-bottom:10px}h3{font-size:18px;margin-top:28px}h1,h2,h3{color:#142a43;scroll-margin-top:24px}
blockquote{margin:0;padding:16px 20px;background:#f0f6fc;border-left:4px solid #4389bf;color:#52627a}p{margin:12px 0}li>p{margin:5px 0}li{margin:6px 0}ul,ol{padding-left:26px}
code{font:0.88em/1.6 Consolas,monospace;background:#f0f3f7;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}.table-wrap{overflow-x:auto;margin:20px 0;padding-bottom:6px}.table-hint{font-size:13px;color:#69788c;margin:12px 0 0}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #e0e6ee;padding:11px 13px;text-align:left}th{background:#edf3f9;white-space:nowrap}tbody tr:nth-child(even){background:#f8fafc}td code{white-space:normal}input{accent-color:#267bb7}.source{font-size:13px;color:#69788c;margin-bottom:24px}.source a{color:#1768ad}
table.wide{width:max-content;min-width:100%;white-space:nowrap;font-variant-numeric:tabular-nums}
pre{padding:18px;background:#f0f3f7;border-radius:8px;overflow-x:auto;line-height:1.6}pre code{padding:0;overflow-wrap:normal}img{max-width:100%;height:auto}
@media(max-width:900px){.layout{display:block;padding:16px}nav{position:static;margin-bottom:22px}nav a{display:inline-block;margin-right:16px}main{padding:24px 18px}h1{font-size:26px}}
@media print{body{background:white}.layout{display:block;padding:0}nav,.source{display:none}main{border:0;padding:0}.table-wrap{overflow:visible}h2,h3{break-after:avoid}tr{break-inside:avoid}}
</style></head><body><div class="layout"><nav aria-label="目录"><strong>__DOCUMENT_TITLE__ · 目录</strong>'''
    page = page.replace('__DOCUMENT_TITLE__', escape(title))
    page += nav + '</nav><main><div class="source">离线阅读版 · 来源：'
    source_link = quote(os.path.relpath(source, output.parent).replace(os.sep, '/'))
    page += f'<a href="{source_link}">{escape(source.name)}</a></div>'
    directory = quote(os.path.relpath(HTML_ROOT / '文档目录.html', output.parent).replace(os.sep, '/'))
    page += f'<p class="source"><a href="{directory}">全部文档目录</a></p>'
    page += body + '</main></div></body></html>\n'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding='utf-8')
    return output


def html_path(source):
    return HTML_ROOT / source.resolve().relative_to(MD_ROOT).with_suffix('.html')


def document_sources():
    return sorted(path for path in MD_ROOT.rglob('*.md') if path.name != '文档目录.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', type=Path, default=SOURCE)
    parser.add_argument('--all', action='store_true', help='Render all project reading documents and build an index')
    args = parser.parse_args()
    if not args.all:
        print(render_document(args.source))
        return
    sources = document_sources()
    lines = ['# 项目文档目录', '', '所有文档均可直接在浏览器中离线阅读。', '', '## 文档与报告', '']
    for source in sources:
        title = next((line[2:].strip() for line in source.read_text(encoding='utf-8-sig').splitlines()
                      if line.startswith('# ')), source.stem)
        relative = source.relative_to(MD_ROOT).as_posix()
        lines.append(f'- [{title}]({relative}) — `{source.relative_to(ROOT).as_posix()}`')
    generated = {html_path(source) for source in sources} | {HTML_ROOT / '文档目录.html'}
    standalone = sorted(path for path in HTML_ROOT.rglob('*.html') if path not in generated)
    if standalone:
        lines += ['', '## 可视化报告', '']
        lines += [f'- [{path.stem}]({os.path.relpath(path, MD_ROOT).replace(os.sep, "/")})' for path in standalone]
    index = MD_ROOT / '文档目录.md'
    index.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    for source in sources + [index]:
        render_document(source)
    print(f'Rendered {len(sources) + 1} documents.')


if __name__ == '__main__':
    main()
