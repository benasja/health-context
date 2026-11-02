"""Build health-context.mcpb, the one-click install for Claude Desktop.

    python3 extension/build.py            # -> dist/health-context.mcpb

An .mcpb is a zip with a manifest.json at the top. Claude Desktop opens it,
asks which folder the export lands in (Downloads unless changed), and runs the
server with uv, which it brings along itself: the person installing it needs
no Python, no terminal and no config file. The server it runs is the same
health_mcp.py as everywhere else, started with --export so it builds its own
data in the background (health_watch.py).

Everything in the manifest that can drift is read from the code: the version
from pyproject.toml, the tool list from health_mcp.TOOLS.
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import health_mcp  # noqa: E402 - needs ROOT on the path first

REPO = 'https://github.com/benasja/health-context'

LONG_DESCRIPTION = """\
Ask Claude about your own Apple Health data: sleep, heart rate, HRV, workouts, \
steps and more, each compared with what is normal *for you* rather than with \
population averages.

**How to use it:** on your iPhone, open Health, tap your profile picture, then \
"Export All Health Data". AirDrop the zip to this Mac (it lands in Downloads) \
and ask Claude something like "How did I sleep last month compared with my best \
month?". The first read takes a minute or two. When you export again later, the \
newer export is picked up by itself.

Everything runs on this computer. The export is never uploaded anywhere by this \
extension; only the short answers to Claude's questions go into your chat.

Not medical advice."""


def version() -> str:
    with open(os.path.join(ROOT, 'pyproject.toml'), encoding='utf-8') as f:
        return re.search(r'^version = "([^"]+)"', f.read(), re.M).group(1)


def modules() -> list[str]:
    with open(os.path.join(ROOT, 'pyproject.toml'), encoding='utf-8') as f:
        block = re.search(r'py-modules = \[(.*?)\]', f.read(), re.S).group(1)
    return [f'{m}.py' for m in re.findall(r'"([^"]+)"', block)]


def first_sentence(text: str) -> str:
    return re.split(r'(?<=[.!?])\s', text.strip(), maxsplit=1)[0]


def manifest() -> dict:
    return {
        'manifest_version': '0.4',
        'name': 'health-context',
        'display_name': 'Apple Health (health-context)',
        'version': version(),
        'description': 'Ask Claude about your Apple Health data, compared with your own normal. '
                       'Runs on your computer.',
        'long_description': LONG_DESCRIPTION,
        'author': {'name': 'Benas Januševičius', 'url': 'https://github.com/benasja'},
        'repository': {'type': 'git', 'url': REPO},
        'homepage': REPO,
        'documentation': f'{REPO}#readme',
        'support': f'{REPO}/issues',
        'icon': 'icon.png',
        'server': {
            'type': 'uv',
            'entry_point': 'health_mcp.py',
            'mcp_config': {
                'command': 'uv',
                'args': ['run', '--directory', '${__dirname}', 'health_mcp.py',
                         '--export', '${user_config.export_folder}'],
            },
        },
        'tools': [{'name': t['name'], 'description': first_sentence(t['description'])}
                  for t in health_mcp.TOOLS],
        'user_config': {
            'export_folder': {
                'type': 'directory',
                'title': 'Where your Apple Health export is',
                'description': 'The folder the export zip lands in. AirDrop to a Mac puts it '
                               'in Downloads. The newest export in it is used, and a newer one '
                               'is picked up by itself.',
                'default': '${DOWNLOADS}',
                'required': True,
            },
        },
        'keywords': ['apple health', 'healthkit', 'apple watch', 'sleep', 'heart rate',
                     'hrv', 'fitness', 'quantified self'],
        'license': 'MIT',
        'compatibility': {
            'platforms': ['darwin', 'win32', 'linux'],
            'runtimes': {'python': '>=3.10'},
        },
    }


# uv runs the server in a virtual environment built from this. No build
# system and package = false: there is nothing to install, only to run.
PYPROJECT = '''[project]
name = "health-context"
version = "{version}"
requires-python = ">=3.10"
dependencies = []

[tool.uv]
package = false
'''


def build(out_path: str) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(manifest(), indent=2, ensure_ascii=False) + '\n')
        z.writestr('pyproject.toml', PYPROJECT.format(version=version()))
        z.write(os.path.join(HERE, 'icon.png'), 'icon.png')
        for name in ('LICENSE', 'profile.example.md', *modules()):
            z.write(os.path.join(ROOT, name), name)
    return out_path


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dist', 'health-context.mcpb')
    path = build(os.path.abspath(out))
    print(f'{path} ({os.path.getsize(path) // 1024} KB, version {version()})')


if __name__ == '__main__':
    main()
