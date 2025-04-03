import re
import markdown
import pymdownx.tasklist
from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor

class InkdropImagePreprocessor(Preprocessor):
    """Преобразует синтаксис ![[filename]] в стандартный Markdown-вид."""
    RE = re.compile(r'!\[\[(.*?)\]\]')

    def run(self, lines):
        new_lines = []
        for line in lines:
            new_line = self.RE.sub(r'![\\1](\\1)', line)
            new_lines.append(new_line)
        return new_lines

class InkdropImageExtension(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.register(
            InkdropImagePreprocessor(md), 'inkdrop_image', 25
        )

def convert_markdown_to_html(markdown_text):
    """
    Преобразует Markdown в HTML с поддержкой Inkdrop-синтаксиса.
    Добавлены необходимые расширения и кастомное расширение.
    """
    md_instance = markdown.Markdown(
        extensions=[
            'tables',
            'toc',
            'fenced_code',
            'codehilite',
            'footnotes',
            'admonition',
            'def_list',
            'attr_list',
            'pymdownx.tasklist',
            InkdropImageExtension()
        ],
        extension_configs={
            'toc': {
                'permalink': False
            },
            'footnotes': {
                'BACKLINK_TEXT': "↩"
            },
            'codehilite': {
                'noclasses': True,
                'linenums': False
            },
            'pymdownx.tasklist': {
                'clickable_checkbox': False,
                'custom_checkbox': True
            }
        }
    )
    html_content = md_instance.convert(markdown_text)
    toc = getattr(md_instance, 'toc', '')
    return html_content, toc

def extract_headers(markdown_text):
    """Извлекает заголовки (уровень, текст) для оглавления из Markdown."""
    headers = []
    lines = markdown_text.split('\n')
    for line in lines:
        if line.startswith('#'):
            match = re.match(r'^#+', line)
            if match:
                level = len(match.group())
                text = line.lstrip('#').strip()
                headers.append((level, text))
    return headers
