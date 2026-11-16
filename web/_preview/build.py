"""生成颜色评审用的静态预览页（不改动产品文件）。"""
import re, pathlib

WEB = pathlib.Path(__file__).resolve().parent.parent
SRC = WEB / 'index.html'
OUT = WEB / '_preview' / 'index.html'

html = SRC.read_text(encoding='utf-8')

# 1) 去掉所有脚本（产品 JS 会连 websocket，预览不需要）
html = re.sub(r'<script[\s\S]*?</script>', '', html)

# 2) 若产品页还没挂主题层，这里补一份（层叠上晚于内联样式）
if 'assets/theme/00-tokens.css' not in html:
    links = ''.join(
        f'<link rel="stylesheet" href="/assets/theme/{f}" />'
        for f in ('00-tokens.css', '10-shell.css', '20-chat.css', '30-workbench.css')
    )
    html = html.replace('</style>', '</style>' + links, 1)

# 3) 注入假数据
html = html.replace('</body>', '<script src="/_preview/mock.js"></script></body>', 1)

OUT.write_text(html, encoding='utf-8')
print('preview written:', OUT, len(html))
