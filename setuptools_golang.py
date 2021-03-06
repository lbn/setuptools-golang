import argparse
import contextlib
import copy
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from distutils.ccompiler import CCompiler
from distutils.dist import Distribution
from typing import Callable
from typing import Dict
from typing import Generator
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Type

from setuptools import Extension
from setuptools.command.build_ext import build_ext as _build_ext


@contextlib.contextmanager
def _tmpdir() -> Generator[str, None, None]:
    tempdir = tempfile.mkdtemp()
    try:
        yield tempdir
    finally:
        def err(
                action: Callable[[str], None],
                name: str,
                exc: Exception,
        ) -> None:  # pragma: no cover (windows)
            """windows: can't remove readonly files, make them writeable!"""
            os.chmod(name, stat.S_IWRITE)
            action(name)

        shutil.rmtree(tempdir, onerror=err)


def _get_cflags(
        compiler: CCompiler,
        macros: Sequence[Tuple[str, Optional[str]]],
) -> str:
    # https://github.com/python/typeshed/pull/3741
    args = [f'-I{p}' for p in compiler.include_dirs]  # type: ignore
    for macro_name, macro_value in macros:
        if macro_value is None:
            args.append(f'-D{macro_name}')
        else:
            args.append(f'-D{macro_name}={macro_value}')
    return ' '.join(args)


LFLAG_CLANG = '-Wl,-undefined,dynamic_lookup'
LFLAG_GCC = '-Wl,--unresolved-symbols=ignore-all'
LFLAGS = (LFLAG_CLANG, LFLAG_GCC)


def _get_ldflags() -> str:
    """Determine the correct link flags.  This attempts dummy compiles similar
    to how autotools does feature detection.
    """
    # windows gcc does not support linking with unresolved symbols
    if sys.platform == 'win32':  # pragma: no cover (windows)
        prefix = getattr(sys, 'real_prefix', sys.prefix)
        libs = os.path.join(prefix, 'libs')
        return '-L{} -lpython{}{}'.format(libs, *sys.version_info[:2])

    cc = subprocess.check_output(('go', 'env', 'CC')).decode('UTF-8').strip()

    with _tmpdir() as tmpdir:
        testf = os.path.join(tmpdir, 'test.c')
        with open(testf, 'w') as f:
            f.write('int f(int); int main(void) { return f(0); }\n')

        for lflag in LFLAGS:  # pragma: no cover (platform specific)
            try:
                subprocess.check_call((cc, testf, lflag), cwd=tmpdir)
                return lflag
            except subprocess.CalledProcessError:
                pass
        else:  # pragma: no cover (platform specific)
            # wellp, none of them worked, fall back to gcc and they'll get a
            # hopefully reasonable error message
            return LFLAG_GCC


def _check_call(cmd: Tuple[str, ...], cwd: str, env: Dict[str, str]) -> None:
    envparts = [f'{k}={shlex.quote(v)}' for k, v in sorted(tuple(env.items()))]
    print(
        '$ {}'.format(' '.join(envparts + [shlex.quote(p) for p in cmd])),
        file=sys.stderr,
    )
    subprocess.check_call(cmd, cwd=cwd, env=dict(os.environ, **env))


def _get_build_extension_method(
        base: Type[_build_ext],
        root: str,
) -> Callable[[_build_ext, Extension], None]:
    def build_extension(self: _build_ext, ext: Extension) -> None:
        # If there are no .go files then the parent should handle this
        if not any(source.endswith('.go') for source in ext.sources):
            # the base class may mutate `self.compiler`
            compiler = copy.deepcopy(self.compiler)
            self.compiler, compiler = compiler, self.compiler
            try:
                return base.build_extension(self, ext)
            finally:
                self.compiler, compiler = compiler, self.compiler

        if len(ext.sources) != 1:
            raise OSError(
                f'Error building extension `{ext.name}`: '
                f'sources must be a single file in the `main` package.\n'
                f'Recieved: {ext.sources!r}',
            )

        main_file, = ext.sources
        if not os.path.exists(main_file):
            raise OSError(
                f'Error building extension `{ext.name}`: '
                f'{main_file} does not exist',
            )
        main_dir = os.path.dirname(main_file)

        # Copy the package into a temporary GOPATH environment
        with _tmpdir() as tempdir:
            root_path = os.path.join(tempdir, 'src', root)
            # Make everything but the last directory (copytree interface)
            os.makedirs(os.path.dirname(root_path))
            shutil.copytree('.', root_path, symlinks=True)
            pkg_path = os.path.join(root_path, main_dir)

            env = {'GOPATH': tempdir}
            cmd_get = ('go', 'get', '-d')
            _check_call(cmd_get, cwd=pkg_path, env=env)

            env.update({
                'CGO_CFLAGS': _get_cflags(
                    self.compiler, ext.define_macros or (),
                ),
                'CGO_LDFLAGS': _get_ldflags(),
            })
            cmd_build = (
                'go', 'build', '-buildmode=c-shared',
                '-o', os.path.abspath(self.get_ext_fullpath(ext.name)),
            )
            _check_call(cmd_build, cwd=pkg_path, env=env)

    return build_extension


def _get_build_ext_cls(base: Type[_build_ext], root: str) -> Type[_build_ext]:
    attrs = {'build_extension': _get_build_extension_method(base, root)}
    return type('build_ext', (base,), attrs)


def set_build_ext(
        dist: Distribution,
        attr: str,
        value: Dict[str, str],
) -> None:
    root = value['root']
    # https://github.com/python/typeshed/pull/3742
    base = dist.cmdclass.get('build_ext', _build_ext)  # type: ignore
    dist.cmdclass['build_ext'] = _get_build_ext_cls(base, root)  # type: ignore


GOLANG = 'https://storage.googleapis.com/golang/go{}.linux-amd64.tar.gz'
SCRIPT = '''\
cd /tmp
curl {golang} --silent --location | tar -xz
export PATH="/tmp/go/bin:$PATH" HOME=/tmp
for py in {pythons}; do
    "/opt/python/$py/bin/pip" wheel --no-deps --wheel-dir /tmp /dist/*.tar.gz
done
ls *.whl | xargs -n1 --verbose auditwheel repair --wheel-dir /dist
ls -al /dist
'''


def build_manylinux_wheels(
        argv: Optional[Sequence[str]] = None,
) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--golang', default='1.13.8',
        help='Override golang version (default %(default)s)',
    )
    parser.add_argument(
        '--pythons', default='cp36-cp36m,cp37-cp37m,cp38-cp38',
        help='Override pythons to build (default %(default)s)',
    )
    args = parser.parse_args(argv)

    golang = GOLANG.format(args.golang)
    pythons = ' '.join(args.pythons.split(','))

    assert os.path.exists('setup.py')
    if os.path.exists('dist'):
        shutil.rmtree('dist')
    os.makedirs('dist')
    _check_call(('python', 'setup.py', 'sdist'), cwd='.', env={})
    _check_call(
        (
            'docker', 'run', '--rm',
            '--volume', f'{os.path.abspath("dist")}:/dist:rw',
            '--user', f'{os.getuid()}:{os.getgid()}',
            'quay.io/pypa/manylinux1_x86_64:latest',
            'bash', '-o', 'pipefail', '-euxc',
            SCRIPT.format(golang=golang, pythons=pythons),
        ),
        cwd='.', env={},
    )
    print('*' * 79)
    print('Your wheels have been built into ./dist')
    print('*' * 79)
    return 0
