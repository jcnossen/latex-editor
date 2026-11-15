"""Deploy the server app to <server_root>/app and restart its systemd unit (config.json: "service", default
"paperreview"). The previous app dir is kept as app.prev. Copies: vps/*.py, hunks.py, prconfig.py, remote/mark.py,
remote/figures.py, vps/mcp_server.py and config.json (the server reads its documents and support files from it).
Also restarts <service>-mcp (the MCP endpoint) when that unit exists.

    python deploy.py            deploy + restart + health check
    python deploy.py --dry-run  list what would be sent
"""
import io, json, os, subprocess, sys, tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prconfig import CFG  # noqa: E402

FILES = {'app.py': 'vps/app.py', 'editor.py': 'vps/editor.py', 'diffdata.py': 'vps/diffdata.py', 'hunks.py': 'hunks.py',
         'prconfig.py': 'prconfig.py', 'mark.py': 'remote/mark.py', 'figures.py': 'remote/figures.py',
         'mcp_server.py': 'vps/mcp_server.py'}


def main():
    root, service, port = CFG['server_root'], CFG.get('service', 'paperreview'), CFG['port']
    files = {name: open(os.path.join(HERE, src), 'rb').read().replace(b'\r\n', b'\n') for name, src in FILES.items()}
    files['config.json'] = open(CFG['_path'], 'rb').read()
    if '--dry-run' in sys.argv:
        print('\n'.join(f'{k}  ({len(v)} bytes)' for k, v in files.items()), f'\n-> {root}/app, restart {service}')
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for name, data in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    app = f'{root}/app'
    cmd = (f'set -e; rm -rf {app}.new; mkdir -p {app}.new; tar --no-same-owner -xf - -C {app}.new; chmod -R a+rX {app}.new; '
           f'if [ -d {app} ]; then rm -rf {app}.prev; mv {app} {app}.prev; fi; mv {app}.new {app}; '
           f'systemctl restart {service}; '
           f'if systemctl cat {service}-mcp >/dev/null 2>&1; then systemctl restart {service}-mcp; fi; '
           f'sleep 1.5; systemctl is-active {service}; '
           f'curl -s -m 10 http://127.0.0.1:{port}/api/editor | head -c 300')
    r = subprocess.run(CFG['ssh'] + [cmd], input=buf.getvalue(), capture_output=True)
    out = r.stdout.decode(errors='replace')
    print(out.strip() or r.stderr.decode(errors='replace'))
    if 'active' not in out or '"ready"' not in out:
        sys.exit(f'deploy: the service did not come up; the previous version is in {app}.prev')


if __name__ == '__main__':
    main()
