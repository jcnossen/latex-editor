"""Project configuration (config.json) shared by the laptop tools and the server app.

Search order: $PRCONFIG, config.json next to this file, config.json in the current directory. See config.example.json.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    'title': 'LaTeX review',
    'repo': '..',                      # the manuscript git repo, relative to the config file
    'baseline_tag': None,              # git tag of the reviewed-against version
    'docs': [],                        # [{key, file, title}]
    'support': [],                     # files besides the .tex the build needs (bib, cls, sty, bst, ...)
    'flatten_markup': False,           # accept \textcolor{blue}{..} / drop \textcolor{red}{\sout{..}} before latexdiff
    'ssh': ['ssh', 'review-host'],     # command prefix that runs a shell command on the server
    'server_root': '/srv/paperreview', # app/, data/, site/, build/ live under this
    'port': 8801,                      # the app's localhost port
}


def find():
    for p in (os.environ.get('PRCONFIG'), os.path.join(HERE, 'config.json'), os.path.join(os.getcwd(), 'config.json')):
        if p and os.path.isfile(p):
            return p
    return None


def load():
    p = find()
    cfg = dict(DEFAULTS)
    if p:
        cfg.update(json.load(open(p, encoding='utf8')))
        cfg['_path'] = p
        cfg['repo_path'] = os.path.normpath(os.path.join(os.path.dirname(p), cfg['repo']))
    else:
        cfg['_path'] = None
        cfg['repo_path'] = os.path.normpath(os.path.join(HERE, cfg['repo']))
    if not cfg['docs']:
        raise SystemExit('no documents configured: copy config.example.json to config.json and fill it in')
    return cfg


CFG = load()
DOCS = {d['key']: d['file'] for d in CFG['docs']}
TITLES = {d['key']: d.get('title', d['key']) for d in CFG['docs']}
SUPPORT = list(CFG['support'])
