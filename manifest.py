#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import pwd
import sys
sys.dont_write_bytecode = True

from copy import deepcopy
from ruamel import yaml
from pathlib import Path

from argparse import ArgumentParser, Action
from contextlib import contextmanager
from fnmatch import fnmatch

SCRIPT_FILE = os.path.abspath(__file__)
SCRIPT_NAME = os.path.basename(SCRIPT_FILE)
SCRIPT_PATH = os.path.dirname(SCRIPT_FILE)
if os.path.islink(__file__):
    REAL_FILE = os.path.abspath(os.readlink(__file__))
    REAL_NAME = os.path.basename(REAL_FILE)
    REAL_PATH = os.path.dirname(REAL_FILE)

SECTIONS = [
    'link_pns',
    'ppa_pns',
    'apt_pns',
    'dnf_pns',
    'npm_pns',
    'pip3_pns',
    'github_pns',
    'script_pns',
]

UID = os.getuid()
GID = pwd.getpwuid(UID).pw_gid
USER = pwd.getpwuid(UID).pw_name

LINKER = '''
linker() {
    file=$(realpath "$1")
    link=$(realpath "$2")
    if [ -f "$link" ] && [ "$file" != "$(readlink $link)" ]; then
        orig="$link.orig"
        $VERBOSE && echo "backing up $orig"
        mv $link $orig
    elif [ ! -f "$link" ] && [ -L "$link" ]; then
        $VERBOSE && echo "removing broken link $link"
        unlink $link
    fi
    if [ -f "$link" ]; then
        echo "[exists] $link"
    else
        echo "[create] $link -> $file"
        mkdir -p $(dirname $link); ln -s $file $link
    fi
}
'''.lstrip('\n').rstrip()

class UnknownPkgmgrError(Exception):
    def __init__(self):
        super(UnknownPkgmgrError, self).__init__('unknown pkgmgr!')

@contextmanager
def cd(*args, **kwargs):
    mkdir = kwargs.pop('mkdir', True)
    verbose = kwargs.pop('verbose', False)
    path = os.path.sep.join(args)
    path = os.path.normpath(path)
    path = os.path.expanduser(path)
    prev = os.getcwd()
    if path != prev:
        if mkdir:
            os.system(f'mkdir -p {path}')
        os.chdir(path)
        curr = os.getcwd()
        sys.path.append(curr)
        if verbose:
            print(f'cd {curr}')
    try:
        yield
    finally:
        if path != prev:
            sys.path.remove(curr)
            os.chdir(prev)
            if verbose:
                print('cd {prev}')

def check_hash(program):
    from subprocess import check_call, CalledProcessError, PIPE
    try:
        check_call(f'hash {program}', shell=True, stdout=PIPE, stderr=PIPE)
        return True
    except CalledProcessError:
        return False

def get_pkgmgr():
    if check_hash('dpkg'):
        return 'deb'
    elif check_hash('rpm'):
        return 'rpm'
    elif check_hash('brew'):
        return 'brew'
    raise UnknownPkgmgrError

class ManifestType():
    def __repr__(self):
        return f'{type(self).__name__}(items = {self.items})'

    __str__ = __repr__

    def functions(self):
        return ''

    def render(self):
        raise NotImplementedError

class Link(ManifestType):
    def __init__(self, spec, patterns, cwd=None, user=None, **kwargs):
        self.cwd = cwd
        self.user = user
        self.recursive = spec.pop('recursive', False)
        def interpolate_rootpath(filepath, rootpath):
            return os.path.realpath(re.sub(f'^{srcpath}', rootpath, filepath))
        def interpolate_user(filepath, user):
            return re.sub(f'USER', user, filepath)
        if self.recursive:
            with cd(cwd):
                self.items = []
                for srcpath, dstpath in spec.items():
                    for item in [item for item in Path(srcpath).rglob('*') if not item.is_dir()]:
                        src = item.as_posix()
                        dst = interpolate_rootpath(src, dstpath)
                        dst = interpolate_user(dst, user)
                        self.items += [(src, dst)]
        else:
            self.items = [(src, interpolate_user(dst, user)) for src, dst in spec.items()]

    def __repr__(self):
        return f'{type(self).__name__}(recursive={self.recursive}, items={self.items})'

    __str__ = __repr__

    def functions(self):
        return LINKER

    def render_items(self):
        return '\n'.join([f'{src} {dst}' for src, dst in self.items])

    def render(self):
        return f'''
echo "link:"
cd {self.cwd}
while read -r file link; do
    linker $file $link
done<<EOM
{self.render_items()}
EOM
        '''.strip()

def sift(items, includes=None):
    if None in (items, includes) or '*' in includes:
        return items
    def match(item, includes):
        return any([fnmatch(item, include) for include in includes])
    return [item for item in items if match(item, includes)]

class PKG(ManifestType):
    def __init__(self, spec, patterns, **kwargs):
        self.items = sift(spec.get('items', None), patterns)

    def __repr__(self):
        return f'{type(self).__name__}(items={self.items})'

    __str__ = __repr__

    def render_items(self):
        return '\n'.join(self.items)

    def render_header(self):
        return f'''
echo "{type(self).__name__.lower()}:"
        '''.strip()

    def render_block(self):
        raise NotImplementedError

    def render(self):
        return f'''
{self.render_header()}

while read pkg; do
{self.render_block()}
done<<EOM
{self.render_items()}
EOM
        '''.strip()

class APT(PKG):
    def render_header(self):
        return f'''
{PKG.render_header(self)}

sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y software-properties-common
        '''.lstrip('\n').rstrip()

    def render_block(self):
        return '''
    sudo apt-get install -y $pkg
        '''.lstrip('\n').rstrip()

class DNF(PKG):
    def render_block(self):
        return '''
    sudo dnf install -y $pkg
        '''.lstrip('\n').rstrip()

class PPA(PKG):
    def render_block(self):
        if not self.items:
            return ''
        return f'''
    ppas=`find /etc/apt/ -name *.list | xargs cat | grep ^[[:space:]]*deb | grep -v deb-src`
    if [[ $ppas != *"$ppa"* ]]; then
        sudo add-apt-repository -y "ppa:$ppa"
    fi
'''.lstrip('\n').rstrip()

class NPM(PKG):
    def render_block(self):
        return f'''
    sudo npm install -g $pkg
'''.lstrip('\n').rstrip()

class PIP3(PKG):
    def render_header(self):
        return f'''
{PKG.render_header(self)}

sudo apt-get install -y python3-dev
sudo -H pip3 install --upgrade pip setuptools
'''.lstrip('\n').rstrip()

    def render_block(self):
        return f'''
    sudo -H pip3 install --upgrade $pkg
'''.lstrip('\n').rstrip()

class Repo():
    def __init__(self, baseurl, reponame, spec, repopath, **kwargs):
        self.baseurl = baseurl
        self.reponame = reponame
        self.repopath = repopath
        self.link = Link(spec.get('link', None), None, **kwargs)

    def __repr__(self):
        return f'{type(self).__name__}(baseurl={self.baseurl}, reponame={self.reponame}, repopath={self.repopath}, link={self.link})'

    __str__ = __repr__

    def render(self):
        return f'''
git clone --recursive {self.baseurl}/{self.reponame} {self.repopath}/{self.reponame}
(cd {self.repopath}/{self.reponame} && pwd && git pull && git checkout HEAD)

{self.link.render()}
    '''.strip()

class Github(ManifestType):
    def __init__(self, spec, patterns, **kwargs):
        repopath = spec.pop('repopath', 'repos')
        self.repos = [Repo('https://github.com', reponame, spec[reponame], repopath, **kwargs) for reponame in sift(spec.keys(), patterns)]

    def __repr__(self):
        return f'{type(self).__name__}(repos={self.repos})'

    __str__ = __repr__

    def functions(self):
        return LINKER

    def render(self):
        if not self.repos:
            return ''
        return 'echo "github:"\n\n' + '\n\n'.join([repo.render() for repo in self.repos])

class Script(ManifestType):
    def __init__(self, spec, patterns, **kwargs):
        self.items = {name: spec[name].strip() for name in sift(spec.keys(), patterns)}

    def __repr__(self):
        return f'{type(self).__name__}(items={self.items})'

    __str__ = __repr__

    def render(self):
        if not self.items:
            return ''
        return 'echo "script:"\n\n' +  '\n\n'.join([f"echo \"{name}:\"\nbash << 'EOM'\n{script}\nEOM" for name, script in self.items.items()])

class Manifest():
    def __init__(
            self,
            spec=None,
            complete=True,
            pkgmgr=None,
            link_pns=None,
            ppa_pns=None,
            apt_pns=None,
            dnf_pns=None,
            npm_pns=None,
            pip3_pns=None,
            github_pns=None,
            script_pns=None,
            **kwargs):
        self.verbose = spec.pop('verbose', False)
        self.errors = spec.pop('errors', False)
        self.sections = []
        if complete or link_pns != None:
            self.sections += [Link(spec['link'], link_pns, **kwargs)]
        if complete or ppa_pns != None:
            self.sections += [PPA(spec['ppa'], ppa_pns, **kwargs)]
        pkgs = spec.get('pkg', {}).get('items', [])
        apts = pkgs + spec.get('apt', {}).get('items', []) if complete or apt_pns != None else []
        dnfs = pkgs + spec.get('dnf', {}).get('items', []) if complete or dnf_pns != None else []
        if pkgmgr == 'deb' and apts:
            self.sections += [APT(dict(items=apts), apt_pns, **kwargs)]
        elif pkgmgr == 'rpm' and dnfs:
            self.sections += [DNF(dict(items=dnfs), dnf_pns, **kwargs)]
        if complete or npm_pns != None:
            self.sections += [NPM(spec['npm'], npm_pns, **kwargs)]
        if complete or pip3_pns != None:
            self.sections += [PIP3(spec['pip3'], pip3_pns, **kwargs)]
        if complete or github_pns != None:
            self.sections += [Github(spec['github'], github_pns, **kwargs)]
        if complete or script_pns != None:
            self.sections += [Script(spec['script'], script_pns, **kwargs)]

    def __repr__(self):
        return f'{type(self).__name__}(verbose={self.verbose}, errors={self.errors}, sections={self.sections})'

    def render(self):
        if not self.sections:
            return ''
        functions = '\n\n'.join(set([section.functions() for section in self.sections])).lstrip('\n').rstrip()
        body = '\n\n'.join([section.render() for section in self.sections]).lstrip('\n').rstrip()
        return f'''
#!/bin/bash
# generated file by manifest.py
# src: https://github.com/scottidler/.../blob/master/manifest.py

{functions}

{body}
        '''.lstrip('\n').rstrip()

    __str__ = __repr__

def load_manifest(complete=True, config=None, **kwargs):
    spec = yaml.safe_load(open(config))
    manifest = Manifest(spec=spec, complete=complete, **kwargs)
    return manifest

def complete(ns):
    return not any([getattr(ns, sec) for sec in SECTIONS])

class ManifestAction(Action):
    def __call__(self, parser, namespace, values, option_strings=None):
        setattr(namespace, self.dest, values if values else ['*'])

def main(args):
    parser = ArgumentParser()
    parser.add_argument(
        '-C', '--config',
        default=f'{SCRIPT_PATH}/manifest.yml',
        help='default=%(default)s; specify the config path')
    parser.add_argument(
        '-D', '--cwd',
        default=os.getcwd(),
        help='default=%(default)s; set the cwd')
    parser.add_argument(
        '-U', '--user',
        default=USER,
        help='default=%(default)s; specify user if not current')
    parser.add_argument(
        '-M', '--pkgmgr',
        default=get_pkgmgr(),
        help=f'default=%(default)s; override pkgmgr')
    parser.add_argument(
        '-l', '--link',
        dest='link_pns',
        metavar='LINK',
	action=ManifestAction,
        nargs='*',
        help='link')
    parser.add_argument(
        '-p', '--ppa',
        dest='ppa_pns',
	action=ManifestAction,
        nargs='*',
        help='ppa')
    parser.add_argument(
        '-a', '--apt',
        dest='apt_pns',
	action=ManifestAction,
        nargs='*',
        help='apt')
    parser.add_argument(
        '-d', '--dnf',
        dest='dnf_pns',
	action=ManifestAction,
        nargs='*',
        help='dnf')
    parser.add_argument(
        '-n', '--npm',
        dest='npm_pns',
	action=ManifestAction,
        nargs='*',
        help='npm')
    parser.add_argument(
        '-P', '--pip3',
        dest='pip3_pns',
	action=ManifestAction,
        nargs='*',
        help='pip3')
    parser.add_argument(
        '-g', '--github',
        dest='github_pns',
	action=ManifestAction,
        nargs='*',
        help='github')
    parser.add_argument(
        '-s', '--script',
        dest='script_pns',
        metavar='SCRIPT',
	action=ManifestAction,
        nargs='*',
        help='script')
    ns = parser.parse_args()
    manifest = load_manifest(complete=complete(ns), **ns.__dict__)
    try:
        print(manifest.render())
        sys.stdout.flush()
    except IOError:
        sys.stderr.write("on running: " + str(sys.exc_info()))
    try:
        sys.stdout.close()
    except IOError:
        sys.stderr.write("on closing: " + str(sys.exc_info()))


if __name__ == '__main__':
    main(sys.argv[1:])
